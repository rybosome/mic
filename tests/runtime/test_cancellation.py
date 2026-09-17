"""Cancellation must preserve results and join every active worker."""

import asyncio
import inspect
import json
import threading
import time
from collections.abc import Coroutine, Sequence
from pathlib import Path

import pytest

import mic


@mic.dataset(name="cancellation-fixtures", schema=mic.case_schema(input=int, expected=int))
def three_rows() -> list[mic.RawCase]:
    return [mic.RawCase(input=i, expected=i, id=f"case-{i}") for i in range(3)]


def read_manifest(path: Path):
    return json.loads((path / "run.json").read_text(encoding="utf-8"))


def read_cases(path: Path):
    return [
        json.loads(line) for line in (path / "cases.jsonl").read_text(encoding="utf-8").splitlines()
    ]


async def test_reporter_cancellation_saves_truthful_upload_outcome(tmp_path: Path) -> None:
    entered = asyncio.Event()
    task_calls: list[int] = []
    reporter_cleanup: list[bool] = []

    class SlowReporter:
        name = "slow"

        async def prepare(self) -> None:
            pass

        async def report(
            self, manifest: mic.JsonObject, cases: Sequence[mic.JsonObject]
        ) -> mic.JsonObject:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                reporter_cleanup.append(True)
            return {"status": "completed"}

    @mic.eval(name="reporter-cancellation", dataset=three_rows, output=int, scorers=[])
    async def identity(ctx: mic.TaskContext[int, mic.JsonObject], value: int) -> int:
        task_calls.append(value)
        return value

    running = asyncio.create_task(mic.arun(identity, output=tmp_path, reporters=[SlowReporter()]))
    await asyncio.wait_for(entered.wait(), timeout=2)
    saved_before = read_cases(tmp_path)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    saved = read_manifest(tmp_path)
    assert saved["status"] == "cancelled"
    assert saved["exit_code"] == 130
    assert saved["reporting"]["slow"]["status"] == "cancelled"
    assert saved["failures"][-1]["phase"] == "reporting"
    assert saved["failures"][-1]["type"] == "CancelledError"
    assert saved["counts"]["completed"] == 3
    assert read_cases(tmp_path) == saved_before
    assert sorted(task_calls) == [0, 1, 2]
    assert reporter_cleanup == [True]
    assert (tmp_path / "report.html").is_file()


@pytest.mark.parametrize("cancel_phase", ["task", "scorer"])
async def test_callback_cancellation_accounts_for_unstarted_jobs(
    tmp_path: Path, cancel_phase: str
) -> None:
    task_calls: list[int] = []

    @mic.scorer(name="exact")
    async def exact(ctx: mic.ScoreContext[int, int, int, mic.JsonObject]) -> int:
        if cancel_phase == "scorer":
            raise asyncio.CancelledError("scorer stopped itself")
        return 1

    @mic.eval(
        name="callback-cancellation",
        dataset=three_rows,
        output=int,
        scorers=[exact],
        concurrency=1,
    )
    async def callback(ctx: mic.TaskContext[int, mic.JsonObject], value: int) -> int:
        task_calls.append(value)
        if cancel_phase == "task":
            raise asyncio.CancelledError("task stopped itself")
        return value

    with pytest.raises(asyncio.CancelledError):
        await mic.arun(callback, output=tmp_path)

    saved, rows = read_manifest(tmp_path), read_cases(tmp_path)
    assert saved["status"] == "cancelled"
    assert saved["exit_code"] == 130
    assert saved["counts"] == {
        "planned": 3,
        "completed": 0,
        "failed": 0,
        "cancelled": 3,
    }
    assert saved["scores"]["exact"]["unavailable_count"] == 3
    assert task_calls == [0]
    assert len(rows) == 1
    assert rows[0]["status"] == "cancelled"
    assert rows[0]["case_id"] == "case-0"
    assert rows[0]["errors"][0]["type"] == "CancelledError"
    assert (tmp_path / "report.html").is_file()


async def test_cancelled_sync_callback_closes_its_returned_coroutine(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    returned: list[Coroutine[object, object, int]] = []
    awaited: list[bool] = []

    async def response() -> int:
        awaited.append(True)
        return 1

    @mic.eval(
        name="mixed-callback-cancellation",
        dataset=three_rows,
        output=int,
        scorers=[],
        concurrency=1,
    )
    def callback(
        ctx: mic.TaskContext[int, mic.JsonObject], value: int
    ) -> Coroutine[object, object, int]:
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(timeout=2):
            raise RuntimeError("Test did not release the callback")
        result = response()
        returned.append(result)
        return result

    running = asyncio.create_task(mic.arun(callback, output=tmp_path))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        running.cancel()
        # Deliver cancellation while the synchronous thread still occupies its slot.
        await asyncio.sleep(0)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert len(returned) == 1
    assert inspect.getcoroutinestate(returned[0]) == inspect.CORO_CLOSED
    assert awaited == []
    saved = read_manifest(tmp_path)
    assert saved["exit_code"] == 130
    assert saved["counts"]["cancelled"] == 3


def test_timed_out_sync_callback_closes_its_returned_coroutine(tmp_path: Path) -> None:
    returned: list[Coroutine[object, object, int]] = []
    awaited: list[bool] = []

    @mic.dataset(name="one", schema=mic.case_schema(input=int, expected=int))
    def one_row() -> list[mic.RawCase]:
        return [mic.RawCase(input=1, expected=1)]

    async def response() -> int:
        awaited.append(True)
        return 1

    @mic.eval(name="mixed-callback-timeout", dataset=one_row, output=int, scorers=[])
    def callback(
        ctx: mic.TaskContext[int, mic.JsonObject], value: int
    ) -> Coroutine[object, object, int]:
        time.sleep(0.025)
        result = response()
        returned.append(result)
        return result

    result = mic.run(callback, output=tmp_path, timeout=0.001)
    assert result.exit_code == 1
    assert result.cases[0]["errors"][0]["type"] == "TimeoutError"
    assert len(returned) == 1
    assert inspect.getcoroutinestate(returned[0]) == inspect.CORO_CLOSED
    assert awaited == []


async def test_repeated_cancellation_joins_sync_worker_and_closes_late_coroutine(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    returned: list[Coroutine[object, object, int]] = []

    async def response() -> int:
        return 1

    @mic.eval(name="repeated-cancel", dataset=three_rows, output=int, scorers=[], concurrency=1)
    def callback(
        ctx: mic.TaskContext[int, mic.JsonObject], value: int
    ) -> Coroutine[object, object, int]:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)
        result = response()
        returned.append(result)
        return result

    running = asyncio.create_task(mic.arun(callback, output=tmp_path))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        for _ in range(3):
            running.cancel()
            await asyncio.sleep(0)
        assert not running.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, timeout=2)
    assert len(returned) == 1
    assert inspect.getcoroutinestate(returned[0]) == inspect.CORO_CLOSED
    saved = read_manifest(tmp_path)
    assert saved["status"] == "cancelled"
    assert saved["exit_code"] == 130
    assert saved["counts"]["cancelled"] == 3
    assert len(read_cases(tmp_path)) == 1


@pytest.mark.parametrize("outcome", ["cancelled", "failed"])
async def test_cleanup_join_propagates_a_workers_own_terminal_failure(outcome: str) -> None:
    from mic._async import drain

    future = asyncio.get_running_loop().create_future()
    if outcome == "cancelled":
        future.cancel()
        expected = asyncio.CancelledError
    else:
        future.set_exception(ValueError("worker failed"))
        expected = ValueError
    with pytest.raises(expected):
        await asyncio.wait_for(drain(future), timeout=1)
