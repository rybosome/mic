"""Progress observers see a run as it happens and can never change its outcome."""

import asyncio
from collections import Counter
from pathlib import Path

import pytest

import mic
from mic._runtime.engine import execute
from mic._runtime.progress import RunPlan, TrialError

from .helpers import evaluation, events, row
from .test_streaming import Capture


class Recorder:
    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.fail_on = fail_on

    def _record(self, name: str, *args: object) -> None:
        self.calls.append((name, args))
        if name == self.fail_on:
            raise RuntimeError("display failed")

    def planned(self, run):
        self._record("planned", run)

    def source_started(self, source_id):
        self._record("source_started", source_id)

    def case_accepted(self, source_id, row_index, label):
        self._record("case_accepted", source_id, row_index, label)

    def record_rejected(self, source_id, row_index, error):
        self._record("record_rejected", source_id, row_index, error)

    def trial_started(self, task, source_id, row_index, trial):
        self._record("trial_started", task, source_id, row_index, trial)

    def trial_finished(self, task, source_id, row_index, trial, observation, errors):
        self._record("trial_finished", task, source_id, row_index, trial, observation, errors)

    def source_finished(self, source_id, summary):
        self._record("source_finished", source_id, summary)

    def cancelling(self):
        self._record("cancelling")

    def finishing(self, sinks):
        self._record("finishing", sinks)

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def args(self, name: str) -> list[tuple[object, ...]]:
        return [args for called, args in self.calls if called == name]


async def test_observer_sees_ordered_progress_for_concurrent_trials() -> None:
    async def slow(_ctx, value):
        await asyncio.sleep(0.001 * (3 - value))
        return value

    spec = evaluation([row(i, id=f"case-{i}") for i in range(3)], task=slow, trials=2)
    observer = Recorder()
    result = await execute(spec, concurrency=4, observer=observer)

    assert result.exit_code == 0
    names = observer.names()
    assert names[0] == "planned"
    assert names[-1] == "finishing"
    assert names.index("source_started") < names.index("case_accepted")
    assert names.index("source_finished") > max(
        i for i, name in enumerate(names) if name == "case_accepted"
    )

    (run,) = observer.args("planned")[0]
    assert isinstance(run, RunPlan)
    assert run.run_id == result.run_id
    assert run.concurrency == 4
    assert [(t.name, t.scorers, t.trials) for t in run.tasks] == [("runtime", ("exact",), 2)]
    (source,) = run.sources
    assert (source.name, source.tasks) == ("fixture", ("runtime",))

    accepted = observer.args("case_accepted")
    assert accepted == [(source.source_id, i, f"case-{i}") for i in range(3)]
    trials = {(row_index, trial) for _, _, row_index, trial in observer.args("trial_started")}
    assert trials == {(i, t) for i in range(3) for t in (1, 2)}

    # Each trial starts once, after its case is accepted, and finishes once, after it starts.
    for name in ("trial_started", "trial_finished"):
        assert Counter(args[2:4] for args in observer.args(name)) == Counter(
            dict.fromkeys(trials, 1)
        )
    for row_index, trial in trials:
        accepted_at = observer.calls.index(
            ("case_accepted", (source.source_id, row_index, f"case-{row_index}"))
        )
        started_at = observer.calls.index(
            ("trial_started", ("runtime", source.source_id, row_index, trial))
        )
        finished_at = next(
            i
            for i, (name, args) in enumerate(observer.calls)
            if name == "trial_finished" and args[2:4] == (row_index, trial)
        )
        assert accepted_at < started_at < finished_at

    assert all(args[4].status == "completed" for args in observer.args("trial_finished"))
    assert all(args[4].scores == {"exact": 1.0} for args in observer.args("trial_finished"))
    ((source_id, summary),) = observer.args("source_finished")
    assert source_id == source.source_id
    assert summary == result.sources[source_id]
    assert observer.args("finishing") == [((),)]
    assert "cancelling" not in names


async def test_observer_receives_generic_failures_and_rejections() -> None:
    @mic.scorer(name="picky")
    def picky(ctx):
        if ctx.output == 2:
            raise KeyError("private scorer detail")
        return 1.0

    def task(_ctx, value):
        if value == 1:
            raise ValueError("private task detail")
        return value

    spec = evaluation([row(1), {}, row(2)], task=task, scorers=[picky])
    observer = Recorder()
    result = await execute(spec, on_invalid="skip", observer=observer)

    assert result.exit_code == 1
    ((source_id, index, error),) = observer.args("record_rejected")
    assert index == 1
    assert (error.phase, error.type) == ("record", "ValueError")
    finished = {args[2]: args for args in observer.args("trial_finished")}
    assert finished[0][4].status == "task_failed"
    assert finished[0][5] == (TrialError("task", "ValueError"),)
    assert finished[2][4].status == "scoring_failed"
    assert finished[2][5] == (TrialError("scorer", "KeyError", "picky"),)
    assert "private" not in repr(observer.calls)


async def test_suite_sources_list_every_evaluation_that_reads_them() -> None:
    first = evaluation([row(1), row(2)])
    second = mic.eval(name="second", dataset=first.dataset, output=object, scorers=[], trials=3)(
        lambda _ctx, value: value
    )
    observer = Recorder()
    result = await execute([first, second], observer=observer)

    assert result.exit_code == 0
    (run,) = observer.args("planned")[0]
    assert [(t.name, t.trials, t.scorers) for t in run.tasks] == [
        ("runtime", 1, ("exact",)),
        ("second", 3, ()),
    ]
    assert [s.tasks for s in run.sources] == [("runtime", "second")]
    started = Counter((args[0], args[2]) for args in observer.args("trial_started"))
    assert started == {("runtime", 0): 1, ("runtime", 1): 1, ("second", 0): 3, ("second", 1): 3}


async def test_finishing_names_only_sinks_still_settling(tmp_path: Path) -> None:
    class BrokenOpen(Capture):
        name = "broken"

        def open(self, info):
            raise RuntimeError("cannot open")

    observer = Recorder()
    await execute(evaluation([row()]), output=tmp_path, sinks=[Capture()], observer=observer)
    assert observer.args("finishing") == [(("jsonl", "capture"),)]

    observer = Recorder()
    await execute(evaluation([row()]), sinks=[Capture(), BrokenOpen()], observer=observer)
    assert observer.args("finishing") == [(("capture",),)]


HOOKS = [
    "planned",
    "source_started",
    "case_accepted",
    "trial_started",
    "trial_finished",
    "source_finished",
    "finishing",
]


def _deterministic(result: mic.RunResult) -> dict[str, object]:
    """Everything a run reports except identifiers and wall-clock timings."""
    trials = result.summary.trials
    return {
        "status": result.status,
        "exit_code": result.exit_code,
        "trials": (trials.planned, trials.completed, trials.task_failed, trials.scoring_failed),
        "scores": {
            name: {metric: (s.count, s.mean, s.min, s.max) for metric, s in t.scores.items()}
            for name, t in result.summary.tasks.items()
        },
        "sources": [
            (s.name, s.records_accepted, s.records_rejected, s.exhausted, s.digest, s.error)
            for s in result.sources.values()
        ],
        "failures": result.failures,
        "sinks": [(s.name, s.status, s.events, s.error) for s in result.sinks],
    }


def _recorded(path: Path) -> list[str]:
    # Source completion may interleave with the last trials, so compare contents, not order.
    return sorted(
        repr(
            (
                event["type"],
                event.get("row_index"),
                event.get("trial"),
                event.get("result", {}).get("status"),
                event.get("result", {}).get("scores"),
            )
        )
        for event in events(path)
    )


@pytest.mark.parametrize("hook", HOOKS)
async def test_a_failing_observer_is_dropped_without_changing_the_run(
    hook: str, tmp_path: Path
) -> None:
    rows = [row(i) for i in range(3)]
    baseline = await execute(evaluation(rows), output=tmp_path / "baseline", concurrency=1)
    observer = Recorder(fail_on=hook)
    observed = await execute(
        evaluation(rows), output=tmp_path / "observed", concurrency=1, observer=observer
    )

    # The observer is never called again after its first error.
    assert observer.names().count(hook) == 1
    assert observer.names()[-1] == hook
    assert observed.exit_code == 0
    assert _deterministic(observed) == _deterministic(baseline)
    assert _recorded(tmp_path / "observed") == _recorded(tmp_path / "baseline")


async def test_cancellation_is_announced_and_every_admitted_trial_finishes() -> None:
    async def blocking(_ctx, value):
        await asyncio.Event().wait()
        return value

    observer = Recorder()
    running = asyncio.create_task(
        execute(
            evaluation([row(i) for i in range(6)], task=blocking),
            concurrency=2,
            observer=observer,
        )
    )

    async def saturated() -> None:
        # Two running, two queued, and the producer blocked admitting row 4.
        while len(observer.args("case_accepted")) < 5 or len(observer.args("trial_started")) < 2:
            await asyncio.sleep(0)

    await asyncio.wait_for(saturated(), timeout=2)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert observer.names().count("cancelling") == 1
    started = {args[2:4] for args in observer.args("trial_started")}
    finished = {args[2:4]: args[4].status for args in observer.args("trial_finished")}
    assert started == {(0, 1), (1, 1)}
    # Queued trials drained by cancellation finish without starting; row 4 was never admitted.
    assert finished == dict.fromkeys([(0, 1), (1, 1), (2, 1), (3, 1)], "cancelled")
    cancelling_at = observer.names().index("cancelling")
    assert "trial_started" not in observer.names()[cancelling_at:]
