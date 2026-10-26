"""Bounded streaming execution with online summaries and opt-in event sinks."""

import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from .._async import drain, run_sync
from ..errors import ConfigurationError, DatasetError
from ..models import Evaluation, JsonObject, JsonValue, ReadLimits
from ..results import EvaluationOutcome, Failure, RunInfo, RunResult, SinkReceipt, failure, to_json
from ..sinks.base import CaseAccepted, RecordRejected, ResultSink, SourceFinished, TrialFinished
from .aggregation import Aggregator
from .callbacks import CallbackPool
from .capture import OUTPUT_FILE, Echo, Origin, OutputCapture, attributed
from .case import cancelled_case, observation, run_case
from .contracts import Case
from .datasets import DatasetReader
from .delivery import Delivery, Scope, Selected
from .files import atomic_write
from .options import resolve_options
from .planning import BoundTask, RunSelection, configuration, plan
from .progress import Progress, RunObserver, RunPlan, SourcePlan, TaskPlan, TrialError
from .recorder import JsonlSink
from .requirements import parse_requirements
from .validation import dumps, json_object, nonempty


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
    capture_output: bool = False,
) -> RunResult:
    """Stream once; no files without output=. Cancellation drains then re-raises."""
    return await execute(
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
        capture_output=capture_output,
    )


async def execute(
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
    capture_output: bool = False,
    observer: RunObserver | None = None,
    echo: Echo | None = None,
) -> RunResult:
    """``arun`` with an internal progress observer and output echo; neither alters the result."""
    run_id = uuid.uuid4().hex
    if not capture_output:
        return await _execute(
            spec,
            run_id,
            None,
            output=output,
            trials=trials,
            concurrency=concurrency,
            require=require,
            sinks=sinks,
            limits=limits,
            max_executions=max_executions,
            timeout=timeout,
            on_invalid=on_invalid,
            observer=observer,
        )
    capture = OutputCapture(run_id, record=output is not None, echo=echo)
    capture.install()
    try:
        return await _execute(
            spec,
            run_id,
            capture,
            output=output,
            trials=trials,
            concurrency=concurrency,
            require=require,
            sinks=sinks,
            limits=limits,
            max_executions=max_executions,
            timeout=timeout,
            on_invalid=on_invalid,
            observer=observer,
        )
    finally:
        capture.close()


async def _execute(
    spec: RunSelection,
    run_id: str,
    capture: OutputCapture | None,
    *,
    output: Path | str | None,
    trials: int | None,
    concurrency: int | None,
    require: Sequence[str],
    sinks: Sequence[ResultSink],
    limits: ReadLimits | None,
    max_executions: int,
    timeout: float | None,
    on_invalid: Literal["abort", "skip"],
    observer: RunObserver | None,
) -> RunResult:
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
    explicit: list[ResultSink] = [recorder] if recorder is not None else []
    explicit.extend(sinks)
    # Declared sinks open once per instance, after explicit sinks, in evaluation order.
    # An instance that is also explicit stays unscoped and receives every event.
    declared: dict[int, tuple[ResultSink, list[str]]] = {}
    explicit_ids = {id(sink) for sink in explicit}
    for task in execution.tasks:
        for sink in task.spec.sinks:
            if id(sink) not in explicit_ids:
                declared.setdefault(id(sink), (sink, []))[1].append(task.spec.name)
    names: set[str] = set()
    for sink in [*explicit, *(sink for sink, _ in declared.values())]:
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

    read_limits = limits or ReadLimits()
    readers = [
        DatasetReader(
            group.dataset,
            f"{run_id}:s{index}",
            read_limits,
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
    source_ids = {
        task.spec.name: reader.source_id
        for reader, group in zip(readers, execution.groups, strict=True)
        for task in group.tasks
    }
    progress = Progress(observer)
    progress(
        lambda o: o.planned(
            RunPlan(
                run_id,
                tuple(
                    TaskPlan(
                        task.spec.name,
                        tuple(metrics[task.spec.name]),
                        task.options.trials,
                        task.options.timeout,
                    )
                    for task in execution.tasks
                ),
                tuple(
                    SourcePlan(
                        reader.source_id,
                        reader.dataset.name,
                        tuple(task.spec.name for task in group.tasks),
                    )
                    for reader, group in zip(readers, execution.groups, strict=True)
                ),
                capacity,
                read_limits,
            )
        )
    )
    selected_sinks = [Selected(sink) for sink in explicit] + [
        Selected(sink, Scope(frozenset(tasks), frozenset(source_ids[name] for name in tasks)))
        for sink, tasks in declared.values()
    ]
    aggregate = Aggregator(metrics)
    # Declared sinks finish with a summary of only their own evaluations.
    scoped = {
        selected.scope: Aggregator({name: metrics[name] for name in selected.scope.tasks})
        for selected in selected_sinks
        if selected.scope is not None
    }
    aggregators = {
        name: [aggregate, *(agg for scope, agg in scoped.items() if name in scope.tasks)]
        for name in metrics
    }
    delivery = Delivery()
    pool = CallbackPool(capacity)
    jobs: asyncio.Queue[_Job | None] = asyncio.Queue(maxsize=capacity)
    interrupted: asyncio.CancelledError | None = None
    failures: list[Failure] = []
    admitted = 0

    def interrupt(exc: asyncio.CancelledError) -> None:
        nonlocal interrupted
        if interrupted is None:
            progress(lambda o: o.cancelling())
        interrupted = exc

    async def join[T](pending: asyncio.Future[T]) -> T:
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError as exc:
            interrupt(exc)
            return await drain(pending)

    async def publish(job: _Job, result: JsonObject) -> None:
        trial = observation(result)
        for aggregator in aggregators[job.task.spec.name]:
            aggregator.observe(job.task.spec.name, trial)
        # Notify before the awaited write so a cancelled write cannot hide a counted trial.
        progress(
            lambda o: o.trial_finished(
                job.task.spec.name,
                job.source_id,
                job.row_index,
                job.trial,
                trial,
                tuple(_trial_error(error) for error in cast(list[JsonValue], result["errors"])),
            )
        )
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
        progress(lambda o: o.source_started(reader.source_id))
        iterator = reader.rows()
        try:
            async for item in iterator:
                if delivery.failed:
                    break
                if len(item) == 2:
                    index, error = item
                    progress(lambda o: o.record_rejected(reader.source_id, index, error))
                    await delivery.write(RecordRejected(reader.source_id, index, error))
                    continue
                index, case, normalized = item
                label = normalized.get("label")
                progress(
                    lambda o: o.case_accepted(
                        reader.source_id, index, label if isinstance(label, str) else None
                    )
                )
                await delivery.write(CaseAccepted(reader.source_id, index, case.id, normalized))
                for task in tasks:
                    for trial in range(1, task.options.trials + 1):
                        if delivery.failed:
                            return
                        if admitted >= max_executions:
                            raise DatasetError(f"Run exceeds max_executions={max_executions}")
                        await jobs.put(_Job(task, reader.source_id, index, trial, case, normalized))
                        for aggregator in aggregators[task.spec.name]:
                            aggregator.admit(task.spec.name)
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
            finished = reader.summary()
            progress(lambda o: o.source_finished(reader.source_id, finished))
            await delivery.write(SourceFinished(reader.source_id, finished))

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
            started = job  # a stable binding keeps the narrowed type inside the closure
            try:
                progress(
                    lambda o: o.trial_started(
                        started.task.spec.name, started.source_id, started.row_index, started.trial
                    )
                )
                origin = Origin(run_id, job.task.spec.name, job.source_id, job.row_index, job.trial)
                try:
                    with attributed(origin):
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
        if capture is not None and destination is not None and recorder is not None:
            if recorder.acquired:
                capture.record_to(destination / OUTPUT_FILE)
        if not delivery.failed:
            workers = [asyncio.create_task(producer())]
            workers.extend(asyncio.create_task(worker()) for _ in range(capacity))
            await asyncio.gather(*workers)
    except asyncio.CancelledError as exc:
        interrupt(exc)
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
    scoped_outcomes = {
        scope: replace(
            outcome,
            summary=aggregator.snapshot(),
            sources={k: v for k, v in outcome.sources.items() if k in scope.sources},
            requirements=tuple(
                result
                for requirement, result in zip(requirements, outcomes, strict=True)
                if requirement.path[0] == "tasks" and requirement.path[1] in scope.tasks
            ),
        )
        for scope, aggregator in scoped.items()
    }
    progress(lambda o: o.finishing(delivery.unfinished))
    finishing = asyncio.create_task(delivery.finish(outcome, scoped_outcomes))
    try:
        receipts = await asyncio.shield(finishing)
    except asyncio.CancelledError as exc:
        interrupt(exc)
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
    if capture is not None:
        # Finish the output record before run.json marks the run as final.
        capture.close()
        if capture.error is not None:
            result = replace(
                result,
                status="cancelled" if result.exit_code == 130 else "failed",
                exit_code=result.exit_code or 1,
                failures=(*result.failures, capture.error),
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


def _trial_error(value: JsonValue) -> TrialError:
    error = json_object(value)
    scorer = error.get("scorer")
    return TrialError(
        str(error["phase"]), str(error["type"]), scorer if isinstance(scorer, str) else None
    )


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
    capture_output: bool = False,
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
            capture_output=capture_output,
        ),
    )
