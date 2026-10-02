"""Evaluation scheduling behavior through the real runner."""

import asyncio
import threading
import time

import pytest

import mic
from tests.runtime.helpers import cases

from .helpers import evaluation, manifest, row


async def test_actual_async_concurrency_bound_and_stable_order(tmp_path):
    active = peak = 0
    completion = []

    async def task(ctx, value):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep((6 - value) * 0.002)
        completion.append(value)
        active -= 1
        return value

    spec = evaluation([row(n, id=str(n)) for n in range(6)], task=task, trials=2)
    result = await mic.arun(spec, output=tmp_path, concurrency=3)
    assert peak == 3
    assert sorted((c["row_index"], c["trial"]) for c in cases(result)) == [
        (i, t) for i in range(6) for t in (1, 2)
    ]
    assert completion != sorted(completion)


async def test_sync_task_does_not_block_event_loop_and_respects_bound(tmp_path):
    active = peak = beats = 0
    lock = threading.Lock()
    done = asyncio.Event()

    def task(ctx, value):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return value

    async def heartbeat():
        nonlocal beats
        while not done.is_set():
            beats += 1
            await asyncio.sleep(0.001)

    pulse = asyncio.create_task(heartbeat())
    try:
        result = await mic.arun(
            evaluation([row(n, id=str(n)) for n in range(4)], task=task),
            output=tmp_path,
            concurrency=2,
        )
    finally:
        done.set()
        await pulse
    assert result.exit_code == 0
    assert peak == 2
    assert beats > 2


async def test_timeout_does_not_admit_more_running_sync_threads(tmp_path):
    active = peak = 0
    lock = threading.Lock()

    def task(ctx, value):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return value

    result = await mic.arun(
        evaluation([row(n, id=str(n)) for n in range(3)], task=task),
        output=tmp_path,
        concurrency=1,
        timeout=0.005,
    )
    assert peak == 1
    assert active == 0
    assert result.summary.trials.task_failed == 3
    assert all(c["errors"][0]["type"] == "TimeoutError" for c in cases(result))


async def test_cancellation_closes_workers_and_preserves_partial_evidence(tmp_path):
    entered = asyncio.Event()
    closed = []

    async def task(ctx, value):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.append(ctx.case_id)

    pending = asyncio.create_task(
        mic.arun(
            evaluation([row(n, id=str(n)) for n in range(4)], task=task),
            output=tmp_path,
            concurrency=2,
        )
    )
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    saved = manifest(tmp_path)
    assert saved["status"] == "cancelled"
    assert saved["exit_code"] == 130
    assert 1 <= saved["summary"]["trials"]["cancelled"] <= 4
    assert closed
    assert (tmp_path / "events.jsonl").is_file()
