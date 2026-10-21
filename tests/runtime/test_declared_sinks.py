"""Sinks declared by an evaluation join a run scoped to that evaluation."""

import json
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from types import ModuleType

import pytest

import mic
from mic.cli import main
from mic.sinks import CaseAccepted, SourceFinished, TrialFinished

from .helpers import evaluation, row
from .test_streaming import Capture


class Named(Capture):
    def __init__(self, name, log=None):
        super().__init__()
        self.name = name
        self.log = log if log is not None else []
        self.opened = 0

    @asynccontextmanager
    async def open(self, info):
        self.opened += 1
        self.log.append(self.name)
        async with super().open(info) as session:
            yield session


def rows(count):
    return [{"id": str(n), "input": n, "expected": n} for n in range(count)]


def suite(first_sinks=(), second_sinks=(), *, shared=False):
    first = replace(evaluation([row()], sinks=list(first_sinks)), name="first")
    second_dataset = first.dataset if shared else evaluation([row()]).dataset
    second = replace(first, name="second", dataset=second_dataset, sinks=tuple(second_sinks))
    return first, second


def test_decorator_stores_declared_sinks_without_opening_them():
    sink = Named("declared")
    spec = evaluation([row()], sinks=[sink])
    assert spec.sinks == (sink,)
    assert evaluation([row()]).sinks == ()
    assert sink.opened == 0
    # Sinks are caller-owned and need not be comparable or hashable.
    assert replace(spec, sinks=()) == spec


def test_declared_sinks_follow_the_recorder_and_explicit_sinks(tmp_path):
    log = []
    declared = Named("declared", log)
    explicit = Named("explicit", log)
    result = mic.run(evaluation([row()], sinks=[declared]), output=tmp_path, sinks=[explicit])
    assert result.exit_code == 0
    assert log == ["explicit", "declared"]
    assert [receipt.name for receipt in result.sinks] == ["jsonl", "explicit", "declared"]
    assert len(declared.trials) == len(explicit.trials) == 1


def test_a_sink_declared_across_a_suite_opens_once_for_its_evaluations():
    sink = Named("shared")
    first, second = suite([sink], [sink])
    result = mic.run([first, second])
    assert result.exit_code == 0
    assert sink.opened == 1
    assert set(sink.info.tasks) == {"first", "second"}
    assert sorted(e.task for e in sink.events if isinstance(e, TrialFinished)) == [
        "first",
        "second",
    ]
    assert [receipt.name for receipt in result.sinks] == ["shared"]


def test_declared_sink_receives_only_its_evaluation():
    declared = Named("declared")
    everything = Capture()
    first, second = suite([declared])
    result = mic.run(
        [first, second],
        sinks=[everything],
        require=[
            'tasks["first"].trials.completed == 1',
            'tasks["second"].trials.completed == 1',
            "trials.completed == 2",
        ],
    )
    assert result.exit_code == 0
    first_source = declared.info.tasks["first"]["source_id"]
    assert set(declared.info.tasks) == {"first"}
    assert {e.task for e in declared.events if isinstance(e, TrialFinished)} == {"first"}
    assert {e.source_id for e in declared.events if not isinstance(e, TrialFinished)} == {
        first_source
    }
    outcome = declared.outcome
    assert set(outcome.summary.tasks) == {"first"}
    assert outcome.summary.trials.planned == 1
    assert set(outcome.sources) == {first_source}
    assert [r.expression for r in outcome.requirements] == ['tasks["first"].trials.completed == 1']
    # The run's status is still run-wide; explicit sinks remain unscoped.
    assert outcome.status == "completed"
    assert set(everything.info.tasks) == {"first", "second"}
    assert len(everything.trials) == 2
    assert everything.outcome.summary.trials.planned == 2
    assert len(everything.outcome.requirements) == 3
    assert result.sinks[1].events == len(declared.events) < result.sinks[0].events


def test_shared_source_events_reach_every_evaluation_that_reads_them():
    declared = Named("declared")
    first, second = suite([], [declared], shared=True)
    assert mic.run([first, second]).exit_code == 0
    assert len([e for e in declared.events if isinstance(e, CaseAccepted)]) == 1
    assert len([e for e in declared.events if isinstance(e, SourceFinished)]) == 1
    assert {e.task for e in declared.events if isinstance(e, TrialFinished)} == {"second"}


def test_an_explicit_sink_that_is_also_declared_opens_once_unscoped():
    sink = Named("both")
    first, second = suite([sink])
    result = mic.run([first, second], sinks=[sink])
    assert result.exit_code == 0
    assert sink.opened == 1
    assert {e.task for e in sink.events if isinstance(e, TrialFinished)} == {"first", "second"}
    assert set(sink.outcome.summary.tasks) == {"first", "second"}


@pytest.mark.parametrize("where", ["suite", "evaluation", "explicit"])
def test_sink_name_collisions_fail_before_reading_sources(where):
    calls = []
    first, second = suite()
    data = replace(first.dataset, factory=lambda: calls.append(True) or [row()])
    first, second = replace(first, dataset=data), replace(second, dataset=data)
    explicit = []
    if where == "suite":
        first, second = replace(first, sinks=(Named("x"),)), replace(second, sinks=(Named("x"),))
    elif where == "evaluation":
        first = replace(first, sinks=(Named("x"), Named("x")))
    else:
        first = replace(first, sinks=(Named("x"),))
        explicit = [Named("x")]
    with pytest.raises(mic.ConfigurationError, match="Duplicate sink name 'x'"):
        mic.run([first, second], sinks=explicit)
    assert calls == []


def test_preflight_validates_declared_sinks_without_opening_them():
    sink = Named("declared")
    assert mic.preflight(evaluation([row()], sinks=[sink]))["status"] == "ready"
    assert sink.opened == 0

    class Nameless:
        name = " "

        def open(self, info):
            raise AssertionError("opened")

    with pytest.raises(mic.ConfigurationError, match="sink name"):
        mic.preflight(evaluation([row()], sinks=[Nameless()]))
    with pytest.raises(mic.ConfigurationError, match="open"):
        mic.preflight(evaluation([row()], sinks=[object()]))


@pytest.mark.parametrize("phase", ["write", "finish"])
def test_declared_sink_failure_semantics_are_unchanged(phase):
    class Broken(Named):
        async def write(self, event):
            if phase == "write" and isinstance(event, TrialFinished):
                raise ValueError("secret")
            await super().write(event)

        async def finish(self, outcome):
            if phase == "finish":
                raise ValueError("secret")
            return await super().finish(outcome)

    healthy = Capture()
    broken = Broken("broken")
    result = mic.run(evaluation(rows(50), sinks=[broken]), sinks=[healthy], concurrency=1)
    assert result.exit_code == 1
    assert healthy.closed and healthy.outcome is not None
    receipt = next(r for r in result.sinks if r.name == "broken")
    assert receipt.status == "failed" and receipt.error.phase == f"sink_{phase}"
    assert "secret" not in json.dumps(result.to_json())
    if phase == "write":
        # A failed write stops admission; admitted work still finishes.
        assert result.summary.trials.planned < 50
        assert result.summary.trials.completed == result.summary.trials.planned
    else:
        assert result.summary.trials.completed == 50


def test_cli_lists_and_can_ignore_declared_sinks(monkeypatch, capsys):
    sink = Named("declared")
    module = ModuleType("declared_sink_fixture")
    module.first, module.second = suite([sink])
    module.data = module.first.dataset
    monkeypatch.setitem(sys.modules, module.__name__, module)

    assert main(["list", module.__name__, "--json"]) == 0
    listed = {r["selector"].split(":")[1]: r for r in json.loads(capsys.readouterr().out)}
    assert listed["first"]["sinks"] == ["declared"]
    assert listed["second"]["sinks"] == []
    assert "sinks" not in listed["data"]
    assert main(["list", module.__name__]) == 0
    assert "sinks=declared" in capsys.readouterr().out
    assert sink.opened == 0

    assert main(["run", f"{module.__name__}:*", "--no-declared-sinks", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["sinks"] == []
    assert sink.opened == 0

    assert main(["run", f"{module.__name__}:*", "--json"]) == 0
    assert [r["name"] for r in json.loads(capsys.readouterr().out)["sinks"]] == ["declared"]
    assert sink.opened == 1
