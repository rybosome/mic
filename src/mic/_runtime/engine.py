"""Bounded streaming execution with online summaries and opt-in event sinks."""

import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from .._async import drain, run_sync
from ..errors import ConfigurationError, DatasetError
from ..models import Evaluation, JsonObject, ReadLimits
from ..results import EvaluationOutcome, Failure, RunInfo, RunResult, SinkReceipt, failure, to_json
from ..sinks.base import CaseAccepted, RecordRejected, ResultSink, SourceFinished, TrialFinished
from .aggregation import Aggregator
from .callbacks import CallbackPool
from .case import cancelled_case, observation, run_case
from .contracts import Case
from .datasets import DatasetReader
from .delivery import Delivery
from .files import atomic_write
from .options import resolve_options
from .planning import BoundTask, RunSelection, configuration, plan
from .recorder import JsonlSink
from .requirements import parse_requirements
from .validation import dumps, nonempty


@dataclass(frozen=True)
class _Job:
    task: BoundTask
    source_id: str
    row_index: int
    trial: int
    case: Case[object, object, object]
    encoded: JsonObject


async def apreflight[I, O, E, M](
    spec: Evaluation[I, O, E, M],
    *,
    trials: int | None = None,
    concurrency: int | None = None,
    limits: ReadLimits | None = None,
    max_executions: int = 50_000,
) -> JsonObject:
    options = resolve_options(
        spec, trials=trials, concurrency=concurrency, timeout=None, max_executions=max_executions
    )
    configuration(spec, options)
    reader = DatasetReader(
        spec.dataset,
        "preflight",
        limits or ReadLimits(),
        require_expected=any(s.requires_expected for s in spec.scorers),
    )
    iterator = reader.rows()
    executions = 0
    try:
        async for item in iterator:
            if len(item) == 2:
                raise DatasetError("Malformed dataset record")
            executions += options.trials
            if executions > max_executions:
                raise DatasetError(f"Dataset exceeds max_executions={max_executions}")
    except (ConfigurationError, DatasetError):
        raise
    except Exception as exc:
        raise DatasetError(f"Source validation failed ({type(exc).__name__})") from None
    finally:
        await iterator.aclose()
    if executions == 0:
        raise DatasetError("Dataset contains no valid trials")
    return {
        "name": spec.name,
        "status": "ready",
        "dataset": to_json(reader.summary()),
        "executions": executions,
        "tasks_executed": 0,
    }


def preflight[I, O, E, M](
    spec: Evaluation[I, O, E, M],
    *,
    trials: int | None = None,
    concurrency: int | None = None,
    limits: ReadLimits | None = None,
    max_executions: int = 50_000,
) -> JsonObject:
    return run_sync(
        "mic.preflight()",
        "mic.apreflight",
        lambda: apreflight(
            spec,
            trials=trials,
            concurrency=concurrency,
            limits=limits,
            max_executions=max_executions,
        ),
    )


async def arun(
    spec: RunSelection,
    *,
    output: Path | str | None = None,
    trials: int | None = None,
    concurrency: int | None = None,
    require: Sequence[str] = (),
    sinks: Sequence[ResultSink] = (),
    limits: ReadLimits | None = None,
    max_executions: int = 50_000,
    timeout: float | None = None,
    on_invalid: Literal["abort", "skip"] = "abort",
) -> RunResult:
    """Stream once; no files without output=. Cancellation drains then re-raises."""
    execution = plan(
        spec, trials=trials, concurrency=concurrency, timeout=timeout, max_executions=max_executions
    )
    capacity = execution.concurrency
    metrics = {task.spec.name: [s.name for s in task.spec.scorers] for task in execution.tasks}
    requirements = parse_requirements(require, metrics)
    if on_invalid not in ("abort", "skip"):
        raise ConfigurationError("on_invalid must be 'abort' or 'skip'")
    destination = Path(output).expanduser().resolve() if output is not None else None
    recorder = JsonlSink(destination) if destination is not None else None
    selected_sinks: list[ResultSink] = [recorder] if recorder is not None else []
    selected_sinks.extend(sinks)
    names: set[str] = set()
    for sink in selected_sinks:
        name = nonempty("sink name", sink.name)
        if name in names:
            raise ConfigurationError(f"Duplicate sink name {name!r}")
        names.add(name)
    if (
        destination is not None
        and destination.exists()
        and (not destination.is_dir() or any(destination.iterdir()))
    ):
        raise ConfigurationError(f"Output directory must be empty: {destination}")

    run_id = uuid.uuid4().hex
    readers = [
        DatasetReader(
            group.dataset,
            f"{run_id}:s{index}",
            limits or ReadLimits(),
            on_invalid=on_invalid,
            require_expected=any(
                s.requires_expected for task in group.tasks for s in task.spec.scorers
            ),
        )
        for index, group in enumerate(execution.groups)
    ]
    info = RunInfo(
        run_id,
        datetime.now(UTC).isoformat(),
        {
            task.spec.name: {
                **task.config,
                "source_id": reader.source_id,
                "limits": to_json(reader.limits),
            }
            for reader, group in zip(readers, execution.groups, strict=True)
            for task in group.tasks
        },
    )
    aggregate = Aggregator(metrics)
    delivery = Delivery()
    pool = CallbackPool(capacity)
    jobs: asyncio.Queue[_Job | None] = asyncio.Queue(maxsize=capacity)
    interrupted: asyncio.CancelledError | None = None
    failures: list[Failure] = []
    admitted = 0

    async def join[T](pending: asyncio.Future[T]) -> T:
        nonlocal interrupted
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError as exc:
            interrupted = exc
            return await drain(pending)

    async def publish(job: _Job, result: JsonObject) -> None:
        aggregate.observe(job.task.spec.name, observation(result))
        await delivery.write(
            TrialFinished(
                job.task.spec.name,
                job.source_id,
                job.row_index,
                job.trial,
                job.case.id,
                result,
            )
        )

    async def produce_source(
        reader: DatasetReader[object, object, object], tasks: tuple[BoundTask, ...]
    ) -> None:
        nonlocal admitted
        iterator = reader.rows()
        try:
            async for item in iterator:
                if delivery.failed:
                    break
                if len(item) == 2:
                    index, error = item
                    await delivery.write(RecordRejected(reader.source_id, index, error))
                    continue
                index, case, normalized = item
                await delivery.write(CaseAccepted(reader.source_id, index, case.id, normalized))
                for task in tasks:
                    for trial in range(1, task.options.trials + 1):
                        if delivery.failed:
                            return
                        if admitted >= max_executions:
                            raise DatasetError(f"Run exceeds max_executions={max_executions}")
                        await jobs.put(_Job(task, reader.source_id, index, trial, case, normalized))
                        aggregate.admit(task.spec.name)
                        admitted += 1
                if delivery.failed:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reader.error = failure("source", exc)
        finally:
            try:
                await iterator.aclose()
            except Exception as exc:
                reader.error = failure("source_close", exc)
            await delivery.write(SourceFinished(reader.source_id, reader.summary()))

    async def producer() -> None:
        cancelled = False
        try:
            for reader, group in zip(readers, execution.groups, strict=True):
                if delivery.failed:
                    break
                await produce_source(reader, group.tasks)
                if admitted >= max_executions and reader.error is not None:
                    break
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            if not cancelled:
                for _ in range(capacity):
                    await jobs.put(None)

    async def worker() -> None:
        while (job := await jobs.get()) is not None:
            try:
                try:
                    result = await run_case(
                        job.task.spec,
                        job.case,
                        job.encoded,
                        job.row_index,
                        job.trial,
                        job.task.options,
                        pool,
                    )
                except asyncio.CancelledError:
                    result = cancelled_case(job.case.id, job.row_index, job.trial, job.encoded)
                await publish(job, result)
                if result["status"] == "cancelled":
                    raise asyncio.CancelledError()
            finally:
                jobs.task_done()

    workers: list[asyncio.Task[None]] = []
    try:
        await delivery.open(selected_sinks, info)
        if not delivery.failed:
            workers = [asyncio.create_task(producer())]
            workers.extend(asyncio.create_task(worker()) for _ in range(capacity))
            await asyncio.gather(*workers)
    except asyncio.CancelledError as exc:
        interrupted = exc
    except Exception as exc:
        failures.append(failure("runtime", exc))
    finally:
        for pending in workers:
            if not pending.done():
                pending.cancel()
        if workers:
            await join(asyncio.gather(*workers, return_exceptions=True))
        while not jobs.empty():
            job = jobs.get_nowait()
            if job is not None:
                await join(
                    asyncio.create_task(
                        publish(
                            job,
                            cancelled_case(
                                job.case.id,
                                job.row_index,
                                job.trial,
                                job.encoded,
                            ),
                        )
                    )
                )
        await join(asyncio.create_task(pool.close()))

    summary = aggregate.snapshot()
    outcomes = tuple(requirement.evaluate(summary) for requirement in requirements)
    if interrupted is None:
        for name, task_summary in summary.tasks.items():
            if task_summary.trials.planned == 0:
                failures.append(
                    Failure(
                        "execution", "EmptyEvaluation", f"No valid trials admitted for {name!r}"
                    )
                )
    exit_code = (
        130
        if interrupted is not None
        else 2
        if any(reader.error is not None for reader in readers)
        else 1
        if failures
        or summary.trials.task_failed
        or summary.trials.scoring_failed
        or not all(r.passed for r in outcomes)
        or delivery.failed
        else 0
    )
    outcome = EvaluationOutcome(
        run_id,
        "cancelled" if interrupted is not None else "failed" if exit_code else "completed",
        exit_code,
        summary,
        {reader.source_id: reader.summary() for reader in readers},
        outcomes,
        tuple(failures),
    )
    finishing = asyncio.create_task(delivery.finish(outcome))
    try:
        receipts = await asyncio.shield(finishing)
    except asyncio.CancelledError as exc:
        interrupted = exc
        finishing.cancel()
        receipts = await drain(finishing)
        outcome = replace(outcome, status="cancelled", exit_code=130)
    if any(receipt.status == "cancelled" for receipt in receipts):
        interrupted = interrupted or asyncio.CancelledError()
        outcome = replace(outcome, status="cancelled", exit_code=130)
    if any(receipt.status != "completed" for receipt in receipts) and outcome.exit_code == 0:
        outcome = replace(outcome, status="failed", exit_code=1)
    result = RunResult(
        outcome.run_id,
        outcome.status,
        outcome.exit_code,
        outcome.summary,
        outcome.sources,
        outcome.requirements,
        outcome.failures,
        receipts,
        destination,
        info,
    )
    # Publish only into an acquired recorder directory, after every sink settled.
    if destination is not None and recorder is not None and recorder.acquired:
        result = _publish_result(destination, result)
    if interrupted is not None:
        interrupted.add_note(
            f"Mic run {run_id} cancelled; admitted work and sinks have been joined."
        )
        raise interrupted
    return result


def _publish_result(destination: Path, result: RunResult) -> RunResult:
    for _ in range(2):
        try:
            atomic_write(destination / "run.json", (dumps(result.to_json()) + "\n",))
            return result
        except Exception as exc:
            receipt = SinkReceipt("manifest", "failed", error=failure("manifest", exc))
            result = replace(
                result,
                status="cancelled" if result.exit_code == 130 else "failed",
                exit_code=result.exit_code or 1,
                sinks=tuple(s for s in result.sinks if s.name != "manifest") + (receipt,),
            )
    return result


def run(
    spec: RunSelection,
    *,
    output: Path | str | None = None,
    trials: int | None = None,
    concurrency: int | None = None,
    require: Sequence[str] = (),
    sinks: Sequence[ResultSink] = (),
    limits: ReadLimits | None = None,
    max_executions: int = 50_000,
    timeout: float | None = None,
    on_invalid: Literal["abort", "skip"] = "abort",
) -> RunResult:
    return run_sync(
        "mic.run()",
        "mic.arun",
        lambda: arun(
            spec,
            output=output,
            trials=trials,
            concurrency=concurrency,
            require=require,
            sinks=sinks,
            limits=limits,
            max_executions=max_executions,
            timeout=timeout,
            on_invalid=on_invalid,
        ),
    )
