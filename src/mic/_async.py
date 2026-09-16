"""Cancellation-safe joins for resources that must outlive their worker."""

import asyncio


async def drain[T](future: asyncio.Future[T]) -> T:
    """Join after cancellation has been recorded, tolerating further cancellation.

    Shielding prevents the caller from cancelling the worker. Cancellation of the
    worker itself still propagates; it cannot make progress and must not be retried.
    """
    while True:
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            if future.done():
                return future.result()
