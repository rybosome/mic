"""A suite shares reads, not mutable trial state or per-task worker pools."""

import asyncio
import json
import sys
from dataclasses import replace
from types import ModuleType

import pytest

import mic
from mic._runtime.discovery import resolve_evaluations
from mic.cli import main
from mic.sinks import CaseAccepted, TrialFinished

from .helpers import evaluation, row
from .test_streaming import Capture


def test_shared_dataset_is_read_and_mapped_once_with_independent_trials():
    reads = []
    mapped = []

    def factory():
        reads.append(True)
        for n in range(3):
            yield {"input": {"values": [n]}, "expected": n}

    def mapper(raw):
        mapped.append(raw)
        return mic.RawCase(input=raw["input"], expected=raw["expected"])

    @mic.dataset(input=dict[str, list[int]], expected=int, map_row=mapper)
    def data():
        return factory()

    @mic.scorer()
    def exact(ctx):
        return float(ctx.output == ctx.require_expected())

    @mic.eval(dataset=data, scorers=[exact], trials=2)
    def mutate(value) -> int:
        original = value["values"][0]
        value["values"].append(999)
        return original

    @mic.eval(dataset=data, scorers=[exact], trials=3)
    def pristine(value) -> int:
        assert len(value["values"]) == 1
        return value["values"][0]

    sink = Capture()
    result = mic.run([mutate, pristine], sinks=[sink], concurrency=2)
    assert result.exit_code == 0
    assert reads == [True] and len(mapped) == 3
    assert len(result.sources) == 1
    assert result.summary.tasks["mutate"].trials.completed == 6
    assert result.summary.tasks["pristine"].trials.completed == 9
    assert result.summary.trials.completed == 15
    accepted = [e for e in sink.events if isinstance(e, CaseAccepted)]
    assert len(accepted) == 3
    assert len({e.case_id for e in sink.events if isinstance(e, TrialFinished)}) == 3
    assert all(t.scores["exact"].mean == 1 for t in result.summary.tasks.values())


async def test_global_concurrency_bounds_shared_source_fanout():
    active = peak = 0
    ready = asyncio.Event()
    release = asyncio.Event()

    async def callback(value):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 3:
            ready.set()
        try:
            await release.wait()
            return value
        finally:
            active -= 1

    first = evaluation([row(i) for i in range(100)], task=callback)
    second = replace(first, name="second")
    pending = asyncio.create_task(mic.arun([first, second], concurrency=3))
    try:
        await asyncio.wait_for(ready.wait(), 5)
        for _ in range(20):
            await asyncio.sleep(0)
        assert active == peak == 3
    finally:
        release.set()
    result = await pending
    assert peak == 3 and active == 0
    assert result.summary.trials.completed == 200
    assert result.summary.trials.task_ms.count == 200
    assert result.summary.trials.scoring_ms.count == 200


def test_equal_but_distinct_datasets_are_not_implicitly_shared():
    calls = []
    first = evaluation([row()])
    source = replace(first.dataset, factory=lambda: calls.append(True) or [row()])
    first = replace(first, dataset=source)
    second = replace(first, name="second", dataset=replace(source))
    result = mic.run([first, second])
    assert result.exit_code == 0
    assert calls == [True, True] and len(result.sources) == 2


def test_failed_source_finishes_its_admitted_work_and_independent_sources():
    def broken():
        yield row()
        raise ValueError("private transport detail")

    first = evaluation(broken())
    shared = replace(first, name="shared")
    independent = replace(evaluation([row(2), row(3)]), name="independent")
    result = mic.run([first, shared, independent], concurrency=1)
    assert result.exit_code == 2
    assert [t.trials.completed for t in result.summary.tasks.values()] == [1, 1, 2]
    assert len(result.sources) == 2
    assert "private transport" not in json.dumps(result.to_json())


def test_requirements_address_each_task_and_the_global_summary():
    first = evaluation([row()])
    second = replace(first, name="other", function=lambda ctx, value: 99, trials=3)
    result = mic.run(
        [first, second],
        require=[
            'tasks["runtime"].scores["exact"].min == 1',
            'tasks["other"].scores["exact"].max == 0',
            "trials.completed == 4",
            "trials.scoring_skipped == 0",
        ],
    )
    assert result.exit_code == 0
    assert all(r.passed for r in result.requirements)


def test_duplicate_names_and_empty_selection_fail_before_effects(tmp_path):
    first = evaluation([row()])
    with pytest.raises(mic.ConfigurationError, match="Duplicate evaluation name"):
        mic.run([first, replace(first)], output=tmp_path / "no-output")
    with pytest.raises(mic.ConfigurationError, match="at least one"):
        mic.run([])
    with pytest.raises(mic.ConfigurationError, match="evaluation definitions"):
        mic.run([first.dataset])
    assert not (tmp_path / "no-output").exists()
    assert mic.run([first, first]).summary.trials.completed == 1


def test_overrides_and_global_execution_cap():
    first = evaluation([row(), row(2)], trials=2, concurrency=1)
    second = replace(first, name="second", trials=3, concurrency=2)
    result = mic.run([first, second], trials=4, max_executions=5)
    assert result.exit_code == 2
    assert result.summary.trials.planned == result.summary.trials.completed == 5
    assert result.info.tasks["runtime"]["options"]["concurrency"] == 2
    assert result.info.tasks["second"]["options"]["trials"] == 4


def test_empty_selected_task_cannot_hide_behind_a_successful_task():
    first = evaluation([])
    second = replace(evaluation([row()]), name="nonempty")
    result = mic.run([first, second])
    assert result.exit_code == 1
    assert result.summary.tasks["nonempty"].trials.completed == 1
    assert result.failures[0].type == "EmptyEvaluation"


async def test_cancelled_suite_counts_admitted_work_only_and_closes_shared_source():
    entered = asyncio.Event()
    closed = []

    async def source():
        try:
            for n in range(1000):
                yield row(n)
        finally:
            closed.append(True)

    async def callback(value):
        entered.set()
        await asyncio.Event().wait()

    first = evaluation(source(), task=callback)
    second = replace(first, name="second")
    sink = Capture()
    pending = asyncio.create_task(mic.arun([first, second], concurrency=2, sinks=[sink]))
    await asyncio.wait_for(entered.wait(), 5)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert closed == [True] and sink.closed
    trials = sink.outcome.summary.trials
    assert 1 <= trials.planned <= 4
    assert trials.cancelled == trials.planned
    assert len(sink.trials) == trials.planned


def test_wildcard_is_deterministic_deduplicated_and_does_not_read_sources(monkeypatch, capsys):
    module = ModuleType("suite_fixture")
    calls = []
    first = evaluation([row()])
    first = replace(
        first, dataset=replace(first.dataset, factory=lambda: calls.append(True) or [row()])
    )
    module.z = first
    module.alias = first
    module.b = replace(first, name="second")
    module._private = replace(first, name="hidden")
    module.data = first.dataset
    monkeypatch.setitem(sys.modules, module.__name__, module)
    assert resolve_evaluations("suite_fixture:*") == [first, module.b]
    assert calls == []
    assert main(["run", "suite_fixture:*", "--json", "--require", "trials.completed == 2"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert list(result["summary"]["tasks"]) == ["runtime", "second"]
    assert calls == [True]
    with pytest.raises(mic.ConfigurationError, match="evaluation"):
        resolve_evaluations("suite_fixture:data")
    with pytest.raises(mic.ConfigurationError, match="module name"):
        resolve_evaluations(":*")
    empty = ModuleType("empty_suite_fixture")
    monkeypatch.setitem(sys.modules, empty.__name__, empty)
    with pytest.raises(mic.ConfigurationError, match="No evaluations"):
        resolve_evaluations("empty_suite_fixture:*")


def test_suite_evidence_is_current_schema_and_reportable(tmp_path):
    from mic.reporters import write_report

    from .artifact_contract import assert_directory

    first = evaluation([row()])
    second = replace(first, name="candidate", trials=2)
    result = mic.run([first, second], output=tmp_path)
    saved, events = assert_directory(tmp_path)
    assert saved == result.to_json()
    assert len([e for e in events if e["type"] == "case_accepted"]) == 1
    assert {e["task"] for e in events if e["type"] == "trial_finished"} == {"runtime", "candidate"}
    assert write_report(tmp_path).is_file()
