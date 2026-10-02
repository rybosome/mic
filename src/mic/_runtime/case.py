"""Execute one isolated task/scoring pipeline without retaining other trials."""

import asyncio
import copy
import time
from typing import cast

from ..models import Evaluation, JsonObject, JsonValue, ScoreContext, TaskContext, TaskResult
from ..results import failure, to_json
from .aggregation import TrialObservation, TrialStatus
from .callbacks import CallbackPool
from .contracts import Case
from .options import Options
from .validation import json_object, normalize_score, serialize, validate


def cancelled_case(case_id: str, row_index: int, trial: int, encoded: JsonObject) -> JsonObject:
    return {
        **copy.deepcopy(encoded),
        "case_id": case_id,
        "row_index": row_index,
        "trial": trial,
        "status": "cancelled",
        "scores": [],
        "errors": [],
        "latency": {"task_ms": None, "scoring_ms": None, "total_ms": None},
    }


def observation(result: JsonObject) -> TrialObservation:
    latency = json_object(result["latency"])
    scores = {
        str(score["name"]): cast(float | None, score["value"])
        for score in cast(list[JsonObject], result["scores"])
    }
    return TrialObservation(
        cast(TrialStatus, result["status"]),
        scores,
        cast(float | None, latency["task_ms"]),
        cast(float | None, latency["scoring_ms"]),
        cast(float | None, latency["total_ms"]),
    )


async def run_case[I, O, E, M](
    spec: Evaluation[I, O, E, M],
    row: Case[I, E, M],
    encoded: JsonObject,
    row_index: int,
    trial: int,
    options: Options,
    pool: CallbackPool,
) -> JsonObject:
    result = cancelled_case(row.id, row_index, trial, encoded)
    result["status"] = "completed"
    scores = cast(list[JsonValue], result["scores"])
    errors = cast(list[JsonValue], result["errors"])
    latencies = json_object(result["latency"])
    result["latency"] = latencies
    started = time.perf_counter()
    phase = "task"
    phase_started = started
    scorer_name: str | None = None

    def record_error(exc: BaseException) -> None:
        error = cast(JsonObject, to_json(failure(phase, exc)))
        if scorer_name is not None:
            error["scorer"] = scorer_name
        errors.append(error)

    try:
        async with asyncio.timeout(options.timeout):
            cloned = copy.deepcopy(row)
            ctx = TaskContext(cloned.id, trial, cloned.metadata)
            raw = await pool.invoke(spec.function, ctx, cloned.input)
            phase = "schema"
            metadata = copy.deepcopy(row.metadata)
            if isinstance(raw, TaskResult):
                enriched = cast(TaskResult[O], raw)
                output_raw = enriched.output
                task_metadata = json_object(enriched.metadata, "$.task_metadata")
                result["task_metadata"] = task_metadata
                merged = (
                    {}
                    if metadata is None
                    else json_object(serialize(spec.dataset.schema.metadata, metadata))
                )
                merged.update(task_metadata)
                metadata = validate(spec.dataset.schema.metadata, merged)
                json_object(serialize(spec.dataset.schema.metadata, metadata), "$.metadata")
            else:
                output_raw = raw
            output = validate(spec.output, output_raw)
            result["output"] = serialize(spec.output, output)
            latencies["task_ms"] = (time.perf_counter() - started) * 1000
            if spec.scorers:
                phase = "scorer"
                phase_started = time.perf_counter()
                for scorer in spec.scorers:
                    scorer_name = scorer.name
                    try:
                        context = ScoreContext(
                            copy.deepcopy(row.input),
                            copy.deepcopy(output),
                            copy.deepcopy(row.expected),
                            copy.deepcopy(metadata),
                            row.id,
                            trial,
                        )
                        raw_score = await pool.invoke(scorer.function, context)
                        scores.append(normalize_score(raw_score, scorer.name))
                    except Exception as exc:
                        result["status"] = "scoring_failed"
                        record_error(exc)
    except asyncio.CancelledError as exc:
        result["status"] = "cancelled"
        record_error(exc)
    except Exception as exc:
        result["status"] = "scoring_failed" if phase == "scorer" else "task_failed"
        record_error(exc)
    finally:
        if latencies["task_ms"] is None:
            latencies["task_ms"] = (time.perf_counter() - started) * 1000
        if phase == "scorer":
            latencies["scoring_ms"] = (time.perf_counter() - phase_started) * 1000
        latencies["total_ms"] = (time.perf_counter() - started) * 1000
    return result
