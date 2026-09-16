"""Execute one isolated trial and preserve its successful scores and failures."""

import asyncio
import copy
import time
from typing import cast

from ..artifacts import failure
from ..models import Case, EvalSpec, JsonObject, JsonValue, ScoreContext, TaskContext, TaskResult
from ..validation import json_object, normalize_scores, serialize, validate
from .callbacks import CallbackPool
from .options import Options


async def run_case[I, O, E, M](
    spec: EvalSpec[I, O, E, M],
    row: Case[I, E, M],
    encoded: JsonObject,
    row_index: int,
    trial: int,
    options: Options,
    pool: CallbackPool,
) -> JsonObject:
    result: JsonObject = copy.deepcopy(encoded)
    result.pop("id", None)
    scores: list[JsonValue] = []
    errors: list[JsonValue] = []
    latencies: JsonObject = {"task_ms": 0.0, "scoring_ms": 0.0, "total_ms": 0.0}
    result.update(
        {
            "case_id": row.id,
            "row_index": row_index,
            "trial": trial,
            "status": "completed",
            "scores": scores,
            "errors": errors,
            "latency": latencies,
            "provenance": copy.deepcopy(row.provenance),
        }
    )
    started = time.perf_counter()
    phase = "task"
    scorer_name: str | None = None
    phase_started = started
    try:
        async with asyncio.timeout(options.timeout):
            cloned = copy.deepcopy(row)
            ctx = TaskContext(
                cloned.id, trial, cloned.expected, cloned.metadata, options.model_preset
            )
            raw = await pool.invoke(spec.__wrapped__, ctx, cloned.input)
            latencies["task_ms"] = (time.perf_counter() - phase_started) * 1000
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
                metadata = validate(
                    spec.dataset.schema.metadata, merged, strict=spec.dataset.schema.strict
                )
                json_object(serialize(spec.dataset.schema.metadata, metadata), "$.metadata")
            else:
                output_raw = raw
            output = validate(spec.output, output_raw, strict=spec.dataset.schema.strict)
            result["output"] = serialize(spec.output, output)
            phase = "scorer"
            phase_started = time.perf_counter()
            for scorer in spec.scorers:
                scorer_name = scorer.name
                context = ScoreContext(
                    copy.deepcopy(row.input),
                    copy.deepcopy(output),
                    copy.deepcopy(row.expected),
                    copy.deepcopy(metadata),
                    row.id,
                    trial,
                )
                raw_scores = await pool.invoke(scorer.__wrapped__, context)
                normalized = normalize_scores(raw_scores, scorer.metrics)
                scores.extend(normalized)
            latencies["scoring_ms"] = (time.perf_counter() - phase_started) * 1000
    except asyncio.CancelledError as exc:
        result["status"] = "cancelled"
        errors.append(
            failure(
                "cancelled",
                exc,
                row_index=row_index,
                case_id=row.id,
                trial=trial,
                scorer=scorer_name,
            )
        )
    except Exception as exc:
        result["status"] = "failed"
        errors.append(
            failure(
                phase, exc, row_index=row_index, case_id=row.id, trial=trial, scorer=scorer_name
            )
        )
    finally:
        if phase == "task" and latencies["task_ms"] == 0:
            latencies["task_ms"] = (time.perf_counter() - phase_started) * 1000
        elif phase == "scorer" and latencies["scoring_ms"] == 0:
            latencies["scoring_ms"] = (time.perf_counter() - phase_started) * 1000
        latencies["total_ms"] = (time.perf_counter() - started) * 1000
    return result
