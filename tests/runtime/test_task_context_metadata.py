"""TaskContext.task_metadata is recorded per trial, including for failed trials."""

import asyncio
import dataclasses

import pytest

import mic

from .artifact_contract import assert_directory
from .helpers import cases, evaluation, row
from .test_streaming import Capture


def only_case(result):
    [case] = cases(result)
    return case


def test_task_context_metadata_is_recorded_for_a_plain_output(tmp_path):
    seen = []

    @mic.scorer(name="metadata")
    def check(ctx):
        seen.append(ctx.metadata)
        return 1

    def task(ctx, value):
        ctx.task_metadata["shared"] = "task"
        ctx.task_metadata["calls"] = [{"n": 1}]
        return value

    spec = evaluation([row(metadata={"shared": "row", "label": "row"})], task=task, scorers=[check])
    result = mic.run(spec, output=tmp_path)
    assert result.exit_code == 0
    case = only_case(result)
    assert case["task_metadata"] == {"shared": "task", "calls": [{"n": 1}]}
    assert case["metadata"] == {"shared": "row", "label": "row"}
    assert seen == [{"shared": "task", "label": "row", "calls": [{"n": 1}]}]


def test_no_writes_and_no_task_result_leave_task_metadata_absent(tmp_path):
    result = mic.run(evaluation([row()]), output=tmp_path)
    assert result.exit_code == 0
    assert "task_metadata" not in only_case(result)


@pytest.mark.parametrize(
    ("written", "returned", "recorded"),
    [
        ({}, {}, {}),
        ({"a": 1, "b": 1}, {"b": 2}, {"a": 1, "b": 2}),
    ],
)
def test_task_result_metadata_wins_over_context_metadata(tmp_path, written, returned, recorded):
    def task(ctx, value):
        ctx.task_metadata.update(written)
        return mic.TaskResult(value, returned)

    result = mic.run(evaluation([row()], task=task), output=tmp_path)
    assert result.exit_code == 0
    assert only_case(result)["task_metadata"] == recorded


@pytest.mark.parametrize("concurrency", [1, 4])
def test_each_trial_starts_with_empty_task_metadata(tmp_path, concurrency):
    entries = []

    async def task(ctx, value):
        entries.append(dict(ctx.task_metadata))
        ctx.task_metadata["trial"] = ctx.trial
        await asyncio.sleep(0)
        return value

    spec = evaluation([row()], task=task, trials=4, concurrency=concurrency)
    result = mic.run(spec, output=tmp_path)
    assert result.exit_code == 0
    assert entries == [{}] * 4
    assert [case["task_metadata"] for case in cases(result)] == [{"trial": t} for t in range(1, 5)]


def test_sync_tasks_and_gather_children_can_write_task_metadata(tmp_path):
    def sync_task(ctx, value):
        ctx.task_metadata["thread"] = True
        return value

    async def child(ctx, key):
        await asyncio.sleep(0)
        ctx.task_metadata[key] = True

    async def fan_out(ctx, value):
        await asyncio.gather(child(ctx, "left"), child(ctx, "right"))
        return value

    sync_result = mic.run(evaluation([row()], task=sync_task), output=tmp_path / "sync")
    fan_result = mic.run(evaluation([row()], task=fan_out), output=tmp_path / "gather")
    assert only_case(sync_result)["task_metadata"] == {"thread": True}
    assert only_case(fan_result)["task_metadata"] == {"left": True, "right": True}


@pytest.mark.parametrize(
    ("failure", "error_phase", "error_type"),
    [
        ("raise", "task", "RuntimeError"),
        ("timeout", "task", "TimeoutError"),
        ("invalid_output", "schema", "SchemaError"),
    ],
)
def test_failed_trials_keep_their_task_metadata(tmp_path, failure, error_phase, error_type):
    @mic.scorer(name="never")
    def never(ctx):
        raise AssertionError("scorers must not run after a task failure")

    async def task(ctx, value):
        ctx.task_metadata["calls"] = ["first"]
        if failure == "raise":
            raise RuntimeError("expected fixture failure")
        if failure == "timeout":
            await asyncio.Event().wait()
        return "not an int"

    spec = evaluation([row()], task=task, scorers=[never], output=int)
    result = mic.run(spec, output=tmp_path, timeout=0.05)
    case = only_case(result)
    assert case["status"] == "task_failed"
    assert [(e["phase"], e["type"]) for e in case["errors"]] == [(error_phase, error_type)]
    assert case["task_metadata"] == {"calls": ["first"]}
    assert case["scores"] == []


async def test_cancelled_trials_keep_their_task_metadata(tmp_path):
    async def task(ctx, value):
        ctx.task_metadata["calls"] = ["before cancellation"]
        raise asyncio.CancelledError("task stopped itself")

    with pytest.raises(asyncio.CancelledError):
        await mic.arun(evaluation([row()], task=task, concurrency=1), output=tmp_path)
    [case] = [e["result"] for e in assert_directory(tmp_path)[1] if e["type"] == "trial_finished"]
    assert case["status"] == "cancelled"
    assert [e["type"] for e in case["errors"]] == ["CancelledError"]
    assert case["task_metadata"] == {"calls": ["before cancellation"]}


@pytest.mark.parametrize("invalid", [("tuple",), float("nan"), {1: "non-string key"}])
def test_invalid_task_metadata_fails_a_successful_trial(tmp_path, invalid):
    def task(ctx, value):
        ctx.task_metadata["bad"] = invalid
        return value

    case = only_case(mic.run(evaluation([row()], task=task), output=tmp_path))
    assert case["status"] == "task_failed"
    assert len(case["errors"]) == 1
    assert case["errors"][0]["phase"] == "schema"
    assert case["errors"][0]["type"] in {"TypeError", "ValueError"}
    assert "task_metadata" not in case
    assert "output" not in case


def test_invalid_task_metadata_never_masks_a_task_failure(tmp_path):
    def task(ctx, value):
        ctx.task_metadata["bad"] = ("tuple",)
        raise RuntimeError("expected fixture failure")

    case = only_case(mic.run(evaluation([row()], task=task), output=tmp_path))
    assert case["status"] == "task_failed"
    assert [(e["phase"], e["type"]) for e in case["errors"]] == [
        ("task", "RuntimeError"),
        ("schema", "TypeError"),
    ]
    assert "task_metadata" not in case


def test_writes_after_the_trial_ends_are_not_recorded(tmp_path):
    background = []

    async def late_write(ctx):
        await asyncio.sleep(0)
        ctx.task_metadata["late"] = True

    async def task(ctx, value):
        ctx.task_metadata["early"] = True
        background.append(asyncio.create_task(late_write(ctx)))
        return value

    @mic.scorer(name="after")
    async def after(ctx):
        # The snapshot is already taken; let the late write land before the trial is saved.
        await background[0]
        return 1

    result = mic.run(evaluation([row()], task=task, scorers=[after]), output=tmp_path)
    assert result.exit_code == 0
    assert only_case(result)["task_metadata"] == {"early": True}


def test_task_context_metadata_reaches_sinks_and_artifacts(tmp_path):
    def task(ctx, value):
        ctx.task_metadata["value"] = value
        if value == 2:
            raise RuntimeError("expected fixture failure")
        return value

    sink = Capture()
    spec = evaluation([row(), row(2, id="b")], task=task)
    result = mic.run(spec, output=tmp_path, sinks=[sink])
    assert result.exit_code == 1
    _, events = assert_directory(tmp_path)
    saved = [e["result"] for e in events if e["type"] == "trial_finished"]
    for trials in (saved, sink.trials):
        by_value = {t["input"]: t for t in trials}
        assert by_value[1]["status"] == "completed"
        assert by_value[1]["task_metadata"] == {"value": 1}
        assert by_value[2]["status"] == "task_failed"
        assert by_value[2]["task_metadata"] == {"value": 2}


def test_task_context_still_constructs_without_task_metadata():
    first = mic.TaskContext("c", 1, None)
    second = mic.TaskContext("c", 2, None)
    assert first.task_metadata == {}
    first.task_metadata["k"] = "v"
    assert second.task_metadata == {}
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.task_metadata = {}  # type: ignore[misc]
