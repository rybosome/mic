"""Streaming is an observable contract, not just an iterator-shaped API."""

import asyncio
import gc
import json
import threading
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace

import pytest

import mic
from mic.results import Failure, SinkReceipt
from mic.sinks import CaseAccepted, TrialFinished

from .helpers import evaluation, row


class Capture:
    name = "capture"

    def __init__(self):
        self.events = []
        self.outcome = None
        self.closed = False

    @asynccontextmanager
    async def open(self, info):
        self.info = info
        try:
            yield self
        finally:
            self.closed = True

    async def write(self, event):
        self.events.append(event)

    async def finish(self, outcome):
        self.outcome = outcome
        return SinkReceipt(self.name, "completed")

    @property
    def trials(self):
        return [event.result for event in self.events if isinstance(event, TrialFinished)]


def test_no_output_means_no_files_and_no_case_collection(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = mic.run(evaluation([row()]))
    assert result.exit_code == 0
    assert result.output_dir is None
    assert not hasattr(result, "cases")
    assert list(tmp_path.iterdir()) == []
    assert result.summary.trials.completed == 1


async def test_tasks_and_scoring_start_before_source_exhaustion() -> None:
    scored = asyncio.Event()

    async def source():
        yield row(1)
        await asyncio.wait_for(scored.wait(), 2)
        yield row(2)

    @mic.scorer()
    async def score(ctx):
        scored.set()
        return 1

    result = await mic.arun(evaluation(source(), scorers=[score]), concurrency=1)
    assert result.summary.trials.completed == 2
    assert next(iter(result.sources.values())).exhausted


async def test_slow_sink_applies_bounded_backpressure() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    seen = []

    async def source():
        for value in range(1000):
            seen.append(value)
            yield row(value)

    class Slow(Capture):
        async def write(self, event):
            if isinstance(event, TrialFinished):
                entered.set()
                await release.wait()
            await super().write(event)

    sink = Slow()
    running = asyncio.create_task(mic.arun(evaluation(source()), concurrency=2, sinks=[sink]))
    await asyncio.wait_for(entered.wait(), 2)
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(seen) <= 6
    release.set()
    result = await running
    assert result.summary.trials.completed == 1000
    assert sink.closed


def test_live_typed_records_do_not_grow_with_dataset_size() -> None:
    alive = weakref.WeakSet()
    peak = 0

    @dataclass(eq=False)
    class Payload:
        value: int

        def __post_init__(self):
            nonlocal peak
            alive.add(self)
            peak = max(peak, len(alive))

    def source():
        for value in range(1000):
            yield {"input": {"value": value}, "expected": value}

    result = mic.run(
        evaluation(
            source(),
            schema=mic.case_schema(input=Payload, expected=int),
            task=lambda value: value.value,
            output=int,
        ),
        concurrency=2,
    )
    gc.collect()
    assert result.summary.trials.completed == 1000
    assert peak <= 12
    assert len(alive) == 0


@pytest.mark.parametrize(
    "policy,completed,rejected,exhausted", [("abort", 1, 1, False), ("skip", 2, 1, True)]
)
def test_bad_records_abort_or_skip_without_erasing_work(
    policy, completed, rejected, exhausted
) -> None:
    sink = Capture()
    result = mic.run(
        evaluation(
            [row(1), {"input": "invalid", "expected": 2}, row(3)],
            schema=mic.case_schema(input=int, expected=int),
            output=int,
        ),
        concurrency=1,
        on_invalid=policy,
        sinks=[sink],
    )
    source = next(iter(result.sources.values()))
    assert result.summary.trials.completed == completed
    assert source.records_rejected == rejected
    assert source.exhausted is exhausted
    assert (result.exit_code == 0) == (policy == "skip")
    assert len(sink.trials) == completed


def test_skip_all_invalid_and_source_iterator_exceptions_still_fail() -> None:
    skipped = mic.run(evaluation([{}]), on_invalid="skip")
    assert skipped.exit_code == 1
    assert skipped.summary.trials.planned == 0

    def rows():
        yield row(1)
        raise RuntimeError("credential-bearing response body")

    failed = mic.run(evaluation(rows()), on_invalid="skip")
    assert failed.exit_code == 2
    assert failed.summary.trials.completed == 1
    assert "credential-bearing" not in json.dumps(failed.to_json())


def test_all_scorers_run_independently_and_summary_counts_are_precise() -> None:
    @mic.scorer()
    def broken(ctx):
        raise ValueError("private response")

    @mic.scorer()
    def skip(ctx):
        return None

    @mic.scorer()
    def good(ctx):
        return mic.Score(0.8, {"derived": 1})

    capture = Capture()
    result = mic.run(evaluation([row()], scorers=[broken, skip, good]), sinks=[capture])
    assert result.exit_code == 1
    trials = result.summary.trials
    assert trials.scoring_failed == trials.scoring_skipped == trials.planned == 1
    scores = result.summary.tasks["runtime"].scores
    assert scores["broken"].count == scores["skip"].count == 0
    assert scores["good"].mean == 0.8
    assert "private response" not in json.dumps(capture.trials)


def test_context_has_metadata_and_identity_but_no_reference() -> None:
    contexts = []

    def task(ctx, value):
        contexts.append(ctx)
        assert ctx.metadata == {"team": "support"}
        assert not hasattr(ctx, "expected")
        assert not hasattr(ctx, "require_expected")
        return value

    result = mic.run(evaluation([row(metadata={"team": "support"})], task=task), trials=2)
    assert result.exit_code == 0
    assert sorted(ctx.trial for ctx in contexts) == [1, 2]
    assert all(ctx.case_id.startswith(result.run_id) for ctx in contexts)


def test_duplicate_provider_labels_are_not_case_identity() -> None:
    capture = Capture()
    result = mic.run(evaluation([row(1), row(2)]), sinks=[capture])
    assert result.exit_code == 0
    assert len({case["case_id"] for case in capture.trials}) == 2
    assert {case["label"] for case in capture.trials} == {"a"}


def test_requirements_use_summary_and_validate_before_effects(tmp_path) -> None:
    calls = []
    spec = evaluation([row()])
    spec = replace(spec, dataset=replace(spec.dataset, factory=lambda: calls.append(1) or [row()]))
    with pytest.raises(mic.ConfigurationError):
        mic.run(
            spec, output=tmp_path / "invalid", require=['tasks["missing"].scores["exact"].mean > 0']
        )
    assert calls == []
    assert not (tmp_path / "invalid").exists()
    result = mic.run(
        spec, require=['tasks["runtime"].scores["exact"].mean >= 1', "trials.task_failed == 0"]
    )
    assert all(item.passed for item in result.requirements)
    assert result.exit_code == 0


async def test_read_time_budget_excludes_task_and_sink_waiting() -> None:
    async def task(value):
        await asyncio.sleep(0.02)
        return value

    result = await mic.arun(
        evaluation([row(i) for i in range(20)], task=task),
        concurrency=1,
        limits=mic.ReadLimits(timeout_seconds=0.2),
    )
    assert result.exit_code == 0
    assert result.summary.trials.completed == 20


def test_limits_stop_new_admission_but_finish_admitted_trials() -> None:
    result = mic.run(evaluation([row(i) for i in range(10)]), max_executions=3, concurrency=1)
    assert result.exit_code == 2
    assert result.summary.trials.planned == result.summary.trials.completed == 3
    assert not next(iter(result.sources.values())).exhausted


async def test_cancelled_stream_accounts_only_admitted_trials_and_joins_threads(tmp_path) -> None:
    entered = threading.Event()
    release = threading.Event()
    closed = []

    def source():
        try:
            for value in range(1000):
                yield row(value)
        finally:
            closed.append(True)

    def task(value):
        entered.set()
        assert release.wait(5)
        return value

    running = asyncio.create_task(
        mic.arun(evaluation(source(), task=task), concurrency=1, output=tmp_path)
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        for _ in range(3):
            running.cancel()
            await asyncio.sleep(0)
        assert not running.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, 5)
    saved = json.loads((tmp_path / "run.json").read_text())
    trials = saved["summary"]["trials"]
    assert saved["status"] == "cancelled"
    assert 1 <= trials["planned"] <= 2
    assert trials["planned"] == trials["cancelled"]
    assert closed == [True]


def test_sink_mutations_do_not_change_other_sinks_or_results() -> None:
    class Mutator(Capture):
        name = "mutator"

        async def write(self, event):
            if isinstance(event, CaseAccepted):
                event.case["input"] = "mutated"
            if isinstance(event, TrialFinished):
                event.result["output"] = "mutated"

        async def finish(self, outcome):
            for source in outcome.sources.values():
                source.provenance["injected"] = True
            return await super().finish(outcome)

    capture = Capture()
    result = mic.run(evaluation([row()]), sinks=[Mutator(), capture])
    assert result.exit_code == 0
    assert capture.trials[0]["output"] == 1
    assert "injected" not in next(iter(result.sources.values())).provenance


@pytest.mark.parametrize("phase", ["open", "write", "finish", "close"])
def test_sink_failure_is_visible_and_healthy_sinks_finalize(phase, tmp_path) -> None:
    class Broken(Capture):
        name = "broken"

        @asynccontextmanager
        async def open(self, info):
            if phase == "open":
                raise ValueError("secret")
            try:
                yield self
            finally:
                if phase == "close":
                    raise ValueError("secret")

        async def write(self, event):
            if phase == "write":
                raise ValueError("secret")

        async def finish(self, outcome):
            if phase == "finish":
                raise ValueError("secret")
            return await super().finish(outcome)

    good = Capture()
    result = mic.run(evaluation([row()]), output=tmp_path, sinks=[good, Broken()])
    assert result.exit_code == 1
    assert good.closed
    assert any(receipt.name == "broken" and receipt.status == "failed" for receipt in result.sinks)
    assert "secret" not in json.dumps(result.to_json())
    assert json.loads((tmp_path / "run.json").read_text())["status"] == "failed"


def test_mapping_time_consumes_source_budget_and_joins_mapper(monkeypatch):
    from types import SimpleNamespace

    from mic._runtime import datasets

    clock = [0.0]
    monkeypatch.setattr(datasets, "time", SimpleNamespace(perf_counter=lambda: clock[0]))
    calls = []

    def slow_map(raw):
        # Advance only the source budget clock, not the host event loop. This
        # also covers callbacks completing before the timeout callback runs.
        clock[0] += 61
        calls.append("mapped")
        return mic.RawCase(input=1, expected=1)

    spec = evaluation([row()], task=lambda value: calls.append("task") or value)
    spec = replace(spec, dataset=replace(spec.dataset, map_row=slow_map))
    result = mic.run(spec)
    assert result.exit_code == 2
    assert calls == ["mapped"]
    assert result.summary.trials.planned == 0


def test_synchronous_sink_factory_failure_has_a_receipt_and_closes_prior_sinks():
    class Broken:
        name = "broken"

        def open(self, info):
            raise RuntimeError("private")

    capture = Capture()
    result = mic.run(evaluation([row()]), sinks=[capture, Broken()])
    assert result.exit_code == 1
    assert capture.closed
    assert result.sinks[-1].error.phase == "sink_open"
    assert result.summary.trials.planned == 0


@pytest.mark.parametrize(
    "details", [[], {"bad": float("nan")}, {"large": "x" * 65537}, {"unicode": "🎤" * 20000}]
)
def test_invalid_sink_receipt_is_failure_not_a_corrupt_manifest(details, tmp_path):
    class Invalid(Capture):
        async def finish(self, outcome):
            return SinkReceipt(self.name, "completed", details=details)

    sink = Invalid()
    result = mic.run(evaluation([row()]), sinks=[sink], output=tmp_path)
    assert sink.closed
    assert result.exit_code == 1
    assert result.sinks[-1].error.phase == "sink_finish"
    assert json.loads((tmp_path / "run.json").read_text()) == result.to_json()


@pytest.mark.parametrize(
    "error",
    [
        Failure("phase", "type", "x" * 65537),
        Failure("phase", "", "message"),
    ],
)
def test_receipt_error_must_fit_the_bounded_serializable_contract(error):
    class Invalid(Capture):
        async def finish(self, outcome):
            return SinkReceipt(self.name, "failed", error=error)

    result = mic.run(evaluation([row()]), sinks=[Invalid()])
    assert result.exit_code == 1
    assert result.sinks[-1].error.phase == "sink_finish"


def test_read_cap_records_consumed_probe_and_effective_source_limits():
    result = mic.run(evaluation([row(), row(2)]), limits=mic.ReadLimits(max_rows=1))
    source_id, source = next(iter(result.sources.items()))
    assert result.exit_code == 2
    assert source.records_seen == 2
    assert source.records_accepted == 1 and source.records_rejected == 0
    assert result.info.tasks["runtime"]["source_id"] == source_id
    assert result.info.tasks["runtime"]["limits"]["max_rows"] == 1
