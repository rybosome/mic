"""Admit bounded trials, journal completion and join workers before returning."""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .._async import drain
from ..models import Evaluation, JsonObject
from .artifacts import append_case
from .callbacks import CallbackPool
from .case import run_case
from .materialization import DatasetSnapshot
from .options import Options


@dataclass
class BatchResult:
    cases: list[JsonObject]
    interrupted: asyncio.CancelledError | None
    error: Exception | None


async def execute[I, O, E, M](
    spec: Evaluation[I, O, E, M],
    snapshot: DatasetSnapshot[I, E, M],
    options: Options,
    destination: Path,
) -> BatchResult:
    planned = len(snapshot.cases) * options.trials
    cases: list[JsonObject] = []
    pool = CallbackPool(options.concurrency)
    next_job = 0
    interrupted: asyncio.CancelledError | None = None
    runtime_error: Exception | None = None

    async def worker() -> None:
        nonlocal next_job
        while next_job < planned:
            sequence = next_job
            next_job += 1
            row_index, trial_index = divmod(sequence, options.trials)
            case = await run_case(
                spec,
                snapshot.cases[row_index],
                snapshot.rows[row_index],
                row_index,
                trial_index + 1,
                options,
                pool,
            )
            cases.append(case)
            append_case(destination / "cases.jsonl", case)
            if case["status"] == "cancelled":
                raise asyncio.CancelledError(f"Case {case['case_id']} was cancelled")

    workers = [asyncio.create_task(worker()) for _ in range(min(options.concurrency, planned))]
    try:
        await asyncio.gather(*workers)
    except asyncio.CancelledError as exc:
        interrupted = exc
        for worker_task in workers:
            if not worker_task.done():
                worker_task.cancel()
        await drain(asyncio.gather(*workers, return_exceptions=True))
    except Exception as exc:
        runtime_error = exc
        for worker_task in workers:
            if not worker_task.done():
                worker_task.cancel()
        await drain(asyncio.gather(*workers, return_exceptions=True))
    finally:
        try:
            await pool.close()
        except asyncio.CancelledError as exc:
            interrupted = exc
    cases.sort(key=lambda value: (cast(int, value["row_index"]), cast(int, value["trial"])))
    return BatchResult(cases, interrupted, runtime_error)
