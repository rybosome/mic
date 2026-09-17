"""Public run/preflight implementation and the local evidence lifecycle."""

import asyncio
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from ..errors import ConfigurationError, DatasetError
from ..models import Evaluation, JsonObject, ReadLimits, RunResult
from ..providers.base import Resolver
from ..reporters.base import Reporter
from .artifacts import (
    atomic_json,
    case_errors,
    code_provenance,
    failure,
    finish_artifacts,
    prepare_directory,
    write_dataset,
)
from .batch import execute
from .materialization import load_dataset
from .options import check_cases, resolve_options
from .reporting import export_results, prepare_reporters
from .summary import evaluate_gates, parse_gates, summarize
from .validation import describe


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def preflight[I, O, E, M](
    spec: Evaluation[I, O, E, M],
    *,
    trials: int | None = None,
    concurrency: int | None = None,
    limits: ReadLimits | None = None,
    max_executions: int = 50_000,
    resolver: Resolver | None = None,
    reporters: Sequence[Reporter] = (),
) -> JsonObject:
    if spec.skip:
        return {"name": spec.name, "status": "skipped", "executions": 0}
    options = resolve_options(
        spec,
        trials=trials,
        concurrency=concurrency,
        model_preset=None,
        timeout=None,
        max_executions=max_executions,
    )
    output_schema = describe(spec.output, "output")
    await prepare_reporters(reporters)
    snapshot = await load_dataset(spec.dataset, limits=limits, resolver=resolver)
    check_cases(spec, snapshot, options)
    return {
        "name": spec.name,
        "status": "ready",
        "dataset": snapshot.summary,
        "executions": len(snapshot.cases) * options.trials,
        "options": options.as_json(),
        "output_schema": output_schema,
        "reporters": [r.name for r in reporters],
        "tasks_executed": 0,
    }


async def arun[I, O, E, M](
    spec: Evaluation[I, O, E, M],
    *,
    output: Path | str | None = None,
    trials: int | None = None,
    concurrency: int | None = None,
    model_preset: str | None = None,
    require: Sequence[str] = (),
    reporters: Sequence[Reporter] = (),
    limits: ReadLimits | None = None,
    max_executions: int = 50_000,
    timeout: float | None = None,
    resolver: Resolver | None = None,
) -> RunResult:
    """Run once and persist proof. Setup errors raise; case/report/gate failures return results.

    Cancellation preserves partial evidence and is re-raised after cleanup. Sync
    callbacks must finish cooperatively; Python cannot terminate their threads.
    """
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:10]
    destination = (
        (Path(output) if output is not None else Path(".mic/runs") / run_id).expanduser().resolve()
    )
    prepare_directory(destination)
    (destination / "cases.jsonl").touch()
    (destination / "dataset.jsonl").touch()
    started = time.perf_counter()
    manifest: JsonObject = {
        "schema_version": "mic-run-v1",
        "run_id": run_id,
        "name": spec.name,
        "status": "running",
        "started_at": _now(),
        "ended_at": None,
        "duration_ms": 0,
        "dataset": {"name": spec.dataset.name, "rows": 0},
        "options": {},
        "counts": {"planned": 0, "completed": 0, "failed": 0, "cancelled": 0, "skipped": 0},
        "scores": {},
        "latency": {},
        "failures": [],
        "gates": [],
        "reporting": {},
        "artifacts": {"dataset": "dataset.jsonl", "cases": "cases.jsonl", "report": "report.html"},
        "provenance": code_provenance(spec.function),
        "exit_code": 0,
    }
    atomic_json(destination / "run.json", manifest)
    cases: list[JsonObject] = []
    planned = 0
    if spec.skip:
        manifest.update({"status": "skipped", "ended_at": _now()})
        finish_artifacts(destination, manifest, cases)
        return RunResult(manifest, cases, destination, 0)
    try:
        options = resolve_options(
            spec,
            trials=trials,
            concurrency=concurrency,
            model_preset=model_preset,
            timeout=timeout,
            max_executions=max_executions,
        )
        gates = parse_gates(spec, require)
        manifest["options"] = options.as_json()
        manifest["output_schema"] = describe(spec.output, "output")
        await prepare_reporters(reporters)
        snapshot = await load_dataset(spec.dataset, limits=limits, resolver=resolver)
        manifest["dataset"] = snapshot.summary
        write_dataset(destination / "dataset.jsonl", snapshot.rows)
        check_cases(spec, snapshot, options)
        planned = len(snapshot.cases) * options.trials
        manifest["counts"] = {
            "planned": planned,
            "completed": 0,
            "failed": 0,
            "cancelled": 0,
            "skipped": 0,
        }
        atomic_json(destination / "run.json", manifest)
    except (ConfigurationError, DatasetError) as exc:
        manifest.update(
            {
                "status": "failed",
                "exit_code": 2,
                "ended_at": _now(),
                "duration_ms": (time.perf_counter() - started) * 1000,
                "failures": [
                    failure("dataset" if isinstance(exc, DatasetError) else "configuration", exc)
                ],
            }
        )
        finish_artifacts(destination, manifest, cases)
        exc.add_note(f"Local evidence: {destination / 'report.html'}")
        raise
    except asyncio.CancelledError as exc:
        manifest.update(
            {
                "status": "cancelled",
                "exit_code": 130,
                "ended_at": _now(),
                "failures": [failure("cancelled", exc)],
            }
        )
        finish_artifacts(destination, manifest, cases)
        raise
    except OSError as exc:
        manifest.update(
            {
                "status": "failed",
                "exit_code": 1,
                "ended_at": _now(),
                "duration_ms": (time.perf_counter() - started) * 1000,
                "failures": [failure("artifact", exc)],
            }
        )
        finish_artifacts(destination, manifest, cases)
        return RunResult(manifest, cases, destination, 1)
    batch = await execute(spec, snapshot, options, destination)
    cases, interrupted, runtime_error = batch.cases, batch.interrupted, batch.error
    summarize(
        spec,
        manifest,
        cases,
        planned,
        cancelled=interrupted is not None or runtime_error is not None,
    )
    manifest["failures"] = case_errors(cases)
    if runtime_error is not None:
        manifest["failures"].append(failure("artifact", runtime_error))
    quality_pass = evaluate_gates(manifest, gates)
    exit_code = (
        130 if interrupted is not None else (1 if manifest["failures"] or not quality_pass else 0)
    )
    manifest.update(
        {
            "status": "cancelled"
            if interrupted is not None
            else "failed"
            if exit_code
            else "completed",
            "exit_code": exit_code,
            "ended_at": _now(),
            "duration_ms": (time.perf_counter() - started) * 1000,
        }
    )
    finish_artifacts(destination, manifest, cases)
    if interrupted is not None:
        raise interrupted
    exit_code = await export_results(reporters, manifest, cases, destination, exit_code)
    return RunResult(manifest, cases, destination, exit_code)


def run[I, O, E, M](
    spec: Evaluation[I, O, E, M],
    *,
    output: Path | str | None = None,
    trials: int | None = None,
    concurrency: int | None = None,
    model_preset: str | None = None,
    require: Sequence[str] = (),
    reporters: Sequence[Reporter] = (),
    limits: ReadLimits | None = None,
    max_executions: int = 50_000,
    timeout: float | None = None,
    resolver: Resolver | None = None,
) -> RunResult:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise ConfigurationError(
            "mic.run() cannot run inside an event loop; use 'await mic.arun(...)'"
        )
    return asyncio.run(
        arun(
            spec,
            output=output,
            trials=trials,
            concurrency=concurrency,
            model_preset=model_preset,
            require=require,
            reporters=reporters,
            limits=limits,
            max_executions=max_executions,
            timeout=timeout,
            resolver=resolver,
        )
    )
