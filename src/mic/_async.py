"""Cancellation-safe joins for resources that must outlive their worker."""

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

from .errors import ConfigurationError


def run_sync[T](
    sync_name: str, async_name: str, factory: Callable[[], Coroutine[Any, Any, T]]
) -> T:
    """Run an async implementation from synchronous code without leaking a coroutine."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise ConfigurationError(
            f"{sync_name} cannot run inside an event loop; use 'await {async_name}(...)'"
        )
    return asyncio.run(factory())


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
