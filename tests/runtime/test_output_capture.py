"""Opt-in capture of application stdout/stderr: suppression, attribution, and recording."""

import asyncio
import contextlib
import io
import sys
import threading
import time
from collections import defaultdict

import pytest

import mic
from mic._runtime import capture as capture_module
from mic._runtime.capture import Origin, OutputCapture, attributed
from mic._runtime.engine import execute
from tests.runtime.artifact_contract import assert_artifact, read_jsonl

from .helpers import evaluation, manifest, row


def records(path):
    lines = read_jsonl(path / "output.jsonl")
    for line in lines:
        assert_artifact("output-v1", line)
    return lines


def by_trial(lines):
    joined: dict[tuple[object, ...], str] = defaultdict(str)
    for line in lines:
        if line["type"] == "output":
            key = (line["row_index"], line["trial"], line["scorer"], line["stream"])
            joined[key] += line["text"]
    return dict(joined)


def test_output_passes_through_unless_capture_is_requested(capsys) -> None:
    def task(_ctx, value):
        print(f"task {value}")
        return value

    result = mic.run(evaluation([row(1)], task=task))
    assert result.exit_code == 0
    assert capsys.readouterr().out == "task 1\n"


def test_captured_output_is_hidden_and_streams_are_restored(capsys) -> None:
    def task(_ctx, value):
        print(f"task {value}")
        print("warning", file=sys.stderr)
        return value

    stdout, stderr = sys.stdout, sys.stderr
    result = mic.run(evaluation([row(1), row(2)], task=task), capture_output=True)
    assert result.exit_code == 0
    assert capsys.readouterr() == ("", "")
    assert (sys.stdout, sys.stderr) == (stdout, stderr)


def test_recorded_output_names_the_trial_and_scorer_that_wrote_it(tmp_path) -> None:
    def task(_ctx, value):
        print(f"task {value}")
        print(f"warn {value}", file=sys.stderr)
        return value

    @mic.scorer(name="loud")
    def loud(ctx):
        print(f"scoring {ctx.output}")
        return 1.0

    spec = evaluation(
        [row(i) for i in range(4)], task=task, scorers=[loud], trials=3, concurrency=4
    )
    result = mic.run(spec, output=tmp_path / "run", capture_output=True)
    assert result.exit_code == 0
    lines = records(tmp_path / "run")
    assert {line["evaluation"] for line in lines} == {"runtime"}
    assert {line["source_id"] for line in lines} == set(result.sources)
    expected: dict[tuple[object, ...], str] = {}
    for value in range(4):
        for trial in (1, 2, 3):
            expected[(value, trial, None, "stdout")] = f"task {value}\n"
            expected[(value, trial, None, "stderr")] = f"warn {value}\n"
            expected[(value, trial, "loud", "stdout")] = f"scoring {value}\n"
    assert by_trial(lines) == expected


def test_async_tasks_and_threads_keep_their_attribution(tmp_path) -> None:
    async def task(_ctx, value):
        await asyncio.sleep(0.01 * (3 - value))
        # A child task inherits the trial's context.
        await asyncio.create_task(asyncio.to_thread(print, f"child {value}"))
        # A bare thread does not, so its output is run-level.
        thread = threading.Thread(target=print, args=(f"thread {value}",))
        thread.start()
        thread.join()
        return value

    spec = evaluation([row(i) for i in range(3)], task=task, concurrency=3)
    mic.run(spec, output=tmp_path / "run", capture_output=True)
    lines = records(tmp_path / "run")
    trials = {(line["row_index"], line["text"]) for line in lines if line["trial"] is not None}
    assert trials == {(i, f"child {i}\n") for i in range(3)}
    untagged = sorted(line["text"] for line in lines if line["evaluation"] is None)
    assert untagged == [f"thread {i}\n" for i in range(3)]


def test_concurrent_partial_lines_never_splice_across_trials(tmp_path) -> None:
    barrier = threading.Barrier(4, timeout=5)

    def task(_ctx, value):
        print(f"a{value}", end="")
        barrier.wait()
        print(f"b{value}")
        print("unterminated", end="")
        return value

    spec = evaluation([row(i) for i in range(4)], task=task, concurrency=4)
    mic.run(spec, output=tmp_path / "run", capture_output=True)
    lines = records(tmp_path / "run")
    # Held text flushes when its trial ends, still attributed and never mixed.
    assert by_trial(lines) == {(i, 1, None, "stdout"): f"a{i}b{i}\nunterminated" for i in range(4)}


def test_output_after_a_timeout_still_belongs_to_its_trial(tmp_path) -> None:
    def task(_ctx, value):
        time.sleep(0.2)
        print("late")
        return value

    spec = evaluation([row(1)], task=task)
    result = mic.run(spec, output=tmp_path / "run", timeout=0.05, capture_output=True)
    assert result.summary.trials.task_failed == 1
    assert by_trial(records(tmp_path / "run")) == {(0, 1, None, "stdout"): "late\n"}


def test_run_level_output_before_recording_starts_is_kept(tmp_path) -> None:
    @mic.dataset(name="noisy", schema=mic.case_schema(input=int, expected=int))
    def data():
        print("reading")
        return [mic.RawCase(input=1, expected=1)]

    spec = mic.eval(name="noisy", dataset=data, output=int, scorers=[])(lambda _c, v: v)
    mic.run(spec, output=tmp_path / "run", capture_output=True)
    (line,) = records(tmp_path / "run")
    assert line["text"] == "reading\n"
    assert line["evaluation"] is None and line["trial"] is None


def test_recording_stops_at_the_byte_cap(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(capture_module, "MAX_RECORDED_BYTES", 10)

    def task(_ctx, value):
        print("12345")
        print("67890")
        print("dropped")
        return value

    result = mic.run(evaluation([row(1)], task=task), output=tmp_path / "run", capture_output=True)
    assert result.exit_code == 0
    lines = records(tmp_path / "run")
    assert [line.get("text") for line in lines] == ["12345\n", None]
    assert lines[-1] == {"schema_version": "mic-output-v1", "type": "truncated", "limit_bytes": 10}


def test_a_failed_output_record_fails_the_run_without_hiding_results(tmp_path, monkeypatch):
    class Broken(io.StringIO):
        def write(self, text: str) -> int:
            raise OSError("disk full")

    opened = OutputCapture.record_to

    def record_to(self, path):
        opened(self, path)
        self._file = Broken()

    monkeypatch.setattr(OutputCapture, "record_to", record_to)

    def task(_ctx, value):
        print("hello")
        return value

    result = mic.run(evaluation([row(1)], task=task), output=tmp_path / "run", capture_output=True)
    assert result.exit_code == 1
    assert result.summary.trials.completed == 1
    assert [failure.phase for failure in result.failures] == ["output"]
    assert "disk full" not in str(result.to_json())
    assert manifest(tmp_path / "run")["failures"][0]["type"] == "OSError"


async def test_echo_receives_attributed_output_and_cannot_fail_the_run() -> None:
    seen: list[tuple[Origin | None, str, str]] = []

    def echo(origin, stream, text):
        seen.append((origin, stream, text))
        raise RuntimeError("display bug")

    def task(_ctx, value):
        print("one")
        print("two")
        return value

    result = await execute(evaluation([row(1)], task=task), capture_output=True, echo=echo)
    assert result.exit_code == 0
    # The echo is dropped after its first error; the run is unaffected.
    ((origin, stream, text),) = seen
    assert (origin and origin.row_index, stream, text) == (0, "stdout", "one\n")


async def test_streams_are_restored_when_a_run_is_cancelled() -> None:
    started = asyncio.Event()

    async def task(_ctx, value):
        started.set()
        await asyncio.sleep(30)
        return value

    stdout = sys.stdout
    running = asyncio.create_task(mic.arun(evaluation([row(1)], task=task), capture_output=True))
    await started.wait()
    assert sys.stdout is not stdout
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert sys.stdout is stdout


async def test_only_one_run_at_a_time_captures_output() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def task(_ctx, value):
        started.set()
        await release.wait()
        return value

    first = asyncio.create_task(mic.arun(evaluation([row(1)], task=task), capture_output=True))
    await started.wait()
    with pytest.raises(mic.ConfigurationError, match="one run at a time"):
        await mic.arun(evaluation([row(1)]), capture_output=True)
    release.set()
    assert (await first).exit_code == 0


def test_the_captured_stream_behaves_like_a_text_stream() -> None:
    target = io.StringIO()
    with contextlib.redirect_stdout(target):
        capture = OutputCapture("run", record=False)
        capture.install()
        try:
            stream = sys.stdout
            assert not stream.isatty()
            with pytest.raises(io.UnsupportedOperation):
                stream.fileno()
            with pytest.raises(TypeError):
                stream.write(b"bytes")  # type: ignore[arg-type]
            stream.close()
            print("hidden")
            # Output from another run's trials is not this run's to capture.
            with attributed(Origin("other", "eval", "source", 0, 1)):
                print("passed through")
        finally:
            capture.close()
        print("after")
    assert target.getvalue() == "passed through\nafter\n"
