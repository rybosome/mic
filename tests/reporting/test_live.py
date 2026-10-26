"""Live CLI progress: what appears, when, and that the terminal is always left clean."""

import io
import sys

import pytest

import mic
from mic._runtime.aggregation import TrialObservation
from mic._runtime.capture import Origin
from mic._runtime.engine import execute
from mic._runtime.progress import RunPlan, SourcePlan, TaskPlan, TrialError
from mic.reporters.live import (
    ASCII,
    LiveDisplay,
    PlainDisplay,
    ProgressState,
    fit,
    glyphs_for,
    progress,
)
from mic.results import Failure


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Captured(io.StringIO):
    encoding = "utf-8"


class Terminal(Captured):
    def isatty(self) -> bool:
        return True


def plan(*sources: tuple[str, tuple[str, ...]], trials=1, limit=None, timeout=None) -> RunPlan:
    names = dict.fromkeys(name for _, tasks in sources for name in tasks)
    return RunPlan(
        "0123456789abcdef",
        tuple(TaskPlan(name, ("exact", "recall"), trials, timeout) for name in names),
        tuple(SourcePlan(f"s{i}", source, tasks) for i, (source, tasks) in enumerate(sources)),
        4,
        mic.ReadLimits(row_count=limit),
    )


def done(status="completed", ms=300.0, **scores) -> TrialObservation:
    return TrialObservation(status, scores, ms, None, ms)


def state(clock: Clock | None = None, **options) -> ProgressState:
    return ProgressState(now=clock or Clock(), **options)


def test_header_summarizes_the_plan() -> None:
    progress_state = state()
    progress_state.planned(plan(("data", ("necessity",)), trials=3, limit=50, timeout=30.0))
    assert progress_state.drain() == [
        "mic run 01234567 · necessity · 3 trials/case · concurrency 4 · timeout 30s · limit 50"
    ]


def test_case_line_waits_for_every_trial_and_orders_marks_by_trial() -> None:
    progress_state = state()
    progress_state.planned(plan(("data", ("necessity",)), trials=3))
    progress_state.drain()
    progress_state.case_accepted("s0", 6, "refund-edge")
    progress_state.trial_finished(
        "necessity",
        "s0",
        6,
        2,
        done("task_failed", 30_400.0),
        (TrialError("task", "TimeoutError"),),
    )
    progress_state.trial_finished("necessity", "s0", 6, 3, done(), ())
    assert progress_state.drain() == []
    progress_state.trial_finished("necessity", "s0", 6, 1, done(), ())
    assert progress_state.drain() == [
        "  ✓✗✓   30.4s  row 6 (refund-edge) · trial 2 task_failed (TimeoutError)"
    ]


def test_failures_group_by_kind_and_name_the_scorer() -> None:
    progress_state = state()
    progress_state.planned(plan(("data", ("necessity",)), trials=3))
    progress_state.drain()
    progress_state.case_accepted("s0", 9, None)
    scorer = (TrialError("scorer", "KeyError", "recall"),)
    progress_state.trial_finished("necessity", "s0", 9, 1, done("scoring_failed"), scorer)
    progress_state.trial_finished("necessity", "s0", 9, 2, done(), ())
    progress_state.trial_finished("necessity", "s0", 9, 3, done("scoring_failed"), scorer)
    assert progress_state.drain() == [
        "  !✓!    0.3s  row 9 · trials 1,3 scoring_failed in recall (KeyError)"
    ]


def test_several_evaluations_and_sources_are_named() -> None:
    progress_state = state()
    progress_state.planned(
        plan(("tickets", ("classify", "triage")), ("logs", ("summarize",)), trials=2)
    )
    progress_state.drain()
    progress_state.case_accepted("s0", 3, None)
    for task in ("classify", "triage"):
        for trial in (1, 2):
            failed = task == "triage" and trial == 2
            progress_state.trial_finished(
                task,
                "s0",
                3,
                trial,
                done("task_failed" if failed else "completed", 9_200.0),
                (TrialError("task", "RuntimeError"),) if failed else (),
            )
    assert progress_state.drain() == [
        "  classify ✓✓ triage ✓✗    9.2s  tickets row 3 · triage trial 2 task_failed (RuntimeError)"
    ]


def test_verbose_lines_show_score_means_and_missing_scores() -> None:
    progress_state = state(scores=True)
    progress_state.planned(plan(("data", ("necessity",)), trials=2))
    progress_state.drain()
    progress_state.case_accepted("s0", 0, None)
    progress_state.trial_finished("necessity", "s0", 0, 1, done(exact=1.0, recall=None), ())
    progress_state.trial_finished("necessity", "s0", 0, 2, done(exact=0.5, recall=None), ())
    assert progress_state.drain() == ["  ✓✓    0.3s  row 0 · exact=0.75 recall=–"]


def test_quiet_case_lines_keep_only_problems() -> None:
    progress_state = state(every_case=False)
    progress_state.planned(plan(("data", ("necessity",))))
    progress_state.drain()
    for row, status in enumerate(("completed", "task_failed")):
        progress_state.case_accepted("s0", row, None)
        error = (TrialError("task", "ValueError"),) if status != "completed" else ()
        progress_state.trial_finished("necessity", "s0", row, 1, done(status), error)
    progress_state.record_rejected("s0", 2, Failure("record", "ValueError", "record failed"))
    assert progress_state.drain() == [
        "  ✗    0.3s  row 1 · trial 1 task_failed (ValueError)",
        "  -          row 2 · rejected (ValueError)",
    ]


def test_close_reports_cases_that_will_never_complete() -> None:
    progress_state = state()
    progress_state.planned(plan(("data", ("necessity",)), trials=2))
    progress_state.drain()
    progress_state.case_accepted("s0", 5, None)
    progress_state.case_accepted("s0", 4, None)
    progress_state.trial_finished("necessity", "s0", 4, 1, done("cancelled", None), ())
    progress_state.trial_finished("necessity", "s0", 4, 2, done("cancelled", None), ())
    progress_state.trial_finished("necessity", "s0", 5, 1, done("cancelled", 1_500.0), ())
    assert progress_state.drain() == ["  ··       –  row 4 · trials 1,2 cancelled"]
    assert progress_state.close() == ["  ··    1.5s  row 5 · trial 1 cancelled · not run"]


def test_status_moves_through_each_phase() -> None:
    clock = Clock()
    progress_state = state(clock)
    assert progress_state.status() == "starting…"
    progress_state.planned(plan(("necessity", ("necessity",)), limit=50))
    progress_state.source_started("s0")
    assert progress_state.status() == "reading necessity…"

    progress_state.case_accepted("s0", 0, None)
    progress_state.trial_started("necessity", "s0", 0, 1)
    clock.now = 2.0
    progress_state.case_accepted("s0", 1, None)
    progress_state.trial_finished("necessity", "s0", 1, 1, done(), ())
    assert progress_state.status() == (
        "2/50 records · 1 trials done · 1/4 running · 0 failed · 0.5/s"
    )

    clock.now = 85.0  # row 0 is now well past both slow-trial thresholds
    assert progress_state.status().endswith(" · row 0 running 1:25")
    assert progress_state.footer(200) == f"{progress_state.glyphs.spinner[0]} " + (
        progress_state.status() + " · 1:25"
    )

    progress_state.cancelling()
    assert progress_state.status() == "cancelling · waiting for 1 running trial to return (row 0)"
    progress_state.trial_finished("necessity", "s0", 0, 1, done("cancelled"), ())
    assert progress_state.status() == "cancelling…"


def test_finishing_names_the_sinks_being_waited_on() -> None:
    progress_state = state()
    progress_state.planned(plan(("data", ("necessity",))))
    progress_state.finishing(("jsonl", "braintrust"))
    assert progress_state.status() == "finishing sinks (jsonl, braintrust)…"
    assert progress_state.phase == "finishing"


def test_status_line_never_wraps_and_labels_cannot_inject_escapes() -> None:
    assert fit("ab界界cd", 5, "…") == "ab界…"
    assert fit("short", 10, "…") == "short"

    progress_state = state()
    progress_state.planned(plan(("data", ("necessity",))))
    progress_state.drain()
    progress_state.case_accepted("s0", 0, "evil\x1b[2J\nlabel")
    progress_state.trial_finished("necessity", "s0", 0, 1, done(), ())
    (line,) = progress_state.drain()
    assert "\x1b" not in line and "\n" not in line
    assert line.endswith("row 0 (evil?[2J?label)")
    assert len(progress_state.footer(20)) <= 20


def test_streams_that_cannot_encode_unicode_get_ascii_glyphs() -> None:
    ascii_stream = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
    assert glyphs_for(ascii_stream) is ASCII
    progress_state = ProgressState(glyphs=ASCII)
    progress_state.planned(plan(("data", ("necessity",)), trials=2))
    progress_state.drain()
    progress_state.case_accepted("s0", 0, None)
    progress_state.trial_finished("necessity", "s0", 0, 1, done(), ())
    progress_state.trial_finished(
        "necessity", "s0", 0, 2, done("task_failed"), (TrialError("task", "ValueError"),)
    )
    assert progress_state.drain() == ["  +x    0.3s  row 0 | trial 2 task_failed (ValueError)"]


def test_live_display_keeps_application_output_off_the_status_line(monkeypatch) -> None:
    terminal = Terminal()
    monkeypatch.setattr(sys, "stderr", terminal)
    progress_state = state()
    display = LiveDisplay(progress_state, terminal)
    display.start()
    try:
        assert sys.stderr is not terminal
        progress_state.planned(plan(("data", ("necessity",))))
        display.render(final=False)
        print("partial", end="", file=sys.stderr)
        print(" line from a task", file=sys.stderr)
        print("unterminated", end="", file=sys.stderr)
    finally:
        display.close()

    assert sys.stderr is terminal
    output = terminal.getvalue()
    erase = "\r\x1b[2K"
    assert "mic run 01234567" in output
    # The status line is erased before application output, which is written whole.
    assert f"{erase}partial line from a task\n" in output
    # Unterminated output is completed on close, and no status line is left behind.
    assert output.endswith("partial line from a task\nunterminated\n")


def test_shown_output_names_its_origin_and_cannot_move_the_cursor() -> None:
    progress_state = state(glyphs=ASCII)
    progress_state.planned(plan(("data", ("necessity", "sufficiency")), trials=2))
    origin = Origin("run", "necessity", "s0", 4, 2)
    lines = progress_state.output_lines(origin, "one\ttab\nloading 10%\rloading 100%\n")
    assert lines == [
        "  necessity | row 4 | trial 2 | one     tab",
        "  necessity | row 4 | trial 2 | loading 100%",
    ]
    scorer = progress_state.output_lines(Origin("run", "necessity", "s0", 4, 1, "recall"), "x")
    assert scorer == ["  necessity | row 4 | trial 1 | recall | x"]
    assert progress_state.output_lines(None, "\x1b[31mred\n") == ["  mic | ?[31mred"]


def test_live_display_shows_output_above_a_cleared_status_line() -> None:
    terminal = Terminal()
    progress_state = state()
    progress_state.planned(plan(("data", ("necessity",))))
    display = LiveDisplay(progress_state, terminal)
    display.render(final=False)
    display.echo(Origin("run", "necessity", "s0", 0, 1), "stdout", "hello\n")
    erase = "\r\x1b[2K"
    output = terminal.getvalue()
    assert f"{erase}  row 0 │ hello\n" in output
    display.render(final=True)
    # Nothing is redrawn after the output, so nothing is left to erase.
    assert terminal.getvalue().endswith(f"{erase}  row 0 │ hello\n")


def test_a_broken_terminal_stops_progress_without_raising() -> None:
    class Broken(Terminal):
        def write(self, text: str) -> int:
            raise OSError("closed")

    display = LiveDisplay(state(), Broken())
    display.start()
    display.close()


def test_plain_display_prints_status_on_phase_changes_without_escapes() -> None:
    stream = io.StringIO()
    progress_state = state()
    display = PlainDisplay(progress_state, stream, heartbeat=3600)
    progress_state.planned(plan(("data", ("necessity",))))
    display.render(final=False)
    progress_state.cancelling()
    display.render(final=False)
    display.render(final=False)
    assert stream.getvalue().splitlines() == [
        "mic run 01234567 · necessity · 1 trial/case · concurrency 4",
        "mic: cancelling…",
    ]


def test_quiet_progress_observes_nothing() -> None:
    stream = io.StringIO()
    with progress(stream, quiet=True, verbose=False, show_output=True) as (observer, echo):
        assert observer is None
        assert echo is None
    assert stream.getvalue() == ""


async def test_shown_output_follows_the_header_in_plain_mode() -> None:
    def task(_ctx, value):
        print(f"task {value}")
        return value

    spec = mic.eval(
        name="shown",
        dataset=mic.dataset(name="shown", schema=mic.case_schema(input=int, expected=int))(
            lambda: [mic.RawCase(input=1, expected=1)]
        ),
        output=int,
        scorers=[],
    )(task)
    stream = Captured()
    with progress(stream, quiet=False, verbose=False, show_output=True) as (observer, echo):
        result = await execute(spec, observer=observer, capture_output=True, echo=echo)
    assert result.exit_code == 0
    lines = stream.getvalue().splitlines()
    assert lines[0].startswith("mic run ")
    assert lines[1] == "  row 0 │ task 1"


async def test_a_real_run_reports_every_case_in_verbose_plain_mode() -> None:
    @mic.dataset(name="live", schema=mic.case_schema(input=int, expected=int))
    def data():
        return [mic.RawCase(input=i, expected=i % 2, id=f"case-{i}") for i in range(3)]

    @mic.scorer(name="exact")
    def exact(ctx):
        return float(ctx.output == ctx.require_expected())

    @mic.eval(name="parity", dataset=data, output=int, scorers=[exact], trials=2)
    def parity(_ctx, value: int) -> int:
        return value % 2

    stream = Captured()
    with progress(stream, quiet=False, verbose=True) as (observer, _):
        result = await execute(parity, observer=observer)

    assert result.exit_code == 0
    lines = stream.getvalue().splitlines()
    assert lines[0].startswith(f"mic run {result.run_id[:8]} · parity · 2 trials/case")
    cases = sorted(line for line in lines[1:] if "row" in line)
    assert len(cases) == 3
    assert all(line.startswith("  ✓✓") and line.endswith("exact=1") for line in cases)
    assert "\x1b" not in stream.getvalue()


@pytest.mark.parametrize("hook", ["planned", "trial_finished"])
async def test_progress_state_failures_never_reach_the_run(hook, monkeypatch) -> None:
    def explode(*args, **kwargs):
        raise RuntimeError("display bug")

    monkeypatch.setattr(ProgressState, hook, explode)
    stream = io.StringIO()
    spec = mic.eval(
        name="safe",
        dataset=mic.dataset(name="safe", schema=mic.case_schema(input=int, expected=int))(
            lambda: [mic.RawCase(input=1, expected=1)]
        ),
        output=int,
        scorers=[],
    )(lambda _ctx, value: value)
    with progress(stream, quiet=False, verbose=False) as (observer, _):
        result = await execute(spec, observer=observer)
    assert result.exit_code == 0
    assert result.summary.trials.completed == 1
    assert "display bug" not in stream.getvalue()
