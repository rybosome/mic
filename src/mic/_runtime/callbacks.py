"""Bounded dispatch of synchronous and asynchronous callbacks."""

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import cast

from .._async import drain


class CallbackPool:
    def __init__(self, concurrency: int) -> None:
        self.executor = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="mic")

    async def invoke[T, **P](
        self,
        fn: Callable[P, T | Awaitable[T]],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> T:
        if inspect.iscoroutinefunction(fn):
            return await cast(Awaitable[T], fn(*args, **kwargs))
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self.executor, partial(fn, *args, **kwargs))
        try:
            result = await asyncio.shield(future)
        except asyncio.CancelledError:
            # A running Python thread cannot be killed. Retain this worker slot
            # and drain it rather than admitting replacements under a false bound.
            try:
                abandoned = await drain(future)
                if inspect.iscoroutine(abandoned):
                    abandoned.close()
                elif isinstance(abandoned, asyncio.Future):
                    abandoned.cancel()
            except Exception:
                pass
            raise
        return await cast(Awaitable[T], result) if inspect.isawaitable(result) else cast(T, result)

    async def close(self) -> None:
        closing = asyncio.create_task(
            asyncio.to_thread(self.executor.shutdown, wait=True, cancel_futures=True)
        )
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError:
            await drain(closing)
            raise
