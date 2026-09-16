"""Read any finite synchronous or asynchronous Python iterable."""

from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator, Iterable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol, cast, runtime_checkable

from mic.errors import ConfigurationError, DatasetError
from mic.models import JsonObject, ReadLimits

from ._io import ReadBudget, in_thread, next_item


@runtime_checkable
class _AsyncClose(Protocol):
    async def aclose(self) -> None: ...


@runtime_checkable
class _Close(Protocol):
    def close(self) -> None: ...


@dataclass
class _MemoryRead:
    source: Iterable[object] | AsyncIterable[object]
    limits: ReadLimits
    provenance: JsonObject
    used: bool = False
    iterator: Iterator[object] | AsyncIterator[object] | None = None

    async def close(self) -> None:
        if isinstance(self.iterator, _AsyncClose):
            await self.iterator.aclose()
        elif isinstance(self.iterator, _Close):
            await in_thread(self.iterator.close)

    async def rows(self) -> AsyncIterator[object]:
        if self.used:
            raise DatasetError("memory: a read may only be iterated once; reopen the factory")
        self.used = True
        budget = ReadBudget(self.limits, "memory")
        if isinstance(self.source, AsyncIterable):
            self.iterator = aiter(self.source)
            async for row in self.iterator:
                budget.add_row()
                yield row
        else:
            iterator = iter(self.source)
            self.iterator = iterator
            while True:
                present, row = await in_thread(lambda: next_item(iterator))
                if not present:
                    break
                budget.add_row()
                yield row


class MemoryLoader:
    """The central materializer checks bytes after typed values are serialized.

    A factory should return a fresh iterable per run. Active iterators are closed
    at context exit, so generator finally blocks execute after early selection or
    failed validation. Ordinary lists and other reusable containers are untouched.
    """

    @asynccontextmanager
    async def open(self, handle: object, *, limits: ReadLimits) -> AsyncGenerator[_MemoryRead]:
        if isinstance(handle, (str, bytes, dict)) or not isinstance(
            handle, (Iterable, AsyncIterable)
        ):
            raise ConfigurationError("memory dataset must be an iterable of rows")
        source = cast(Iterable[object] | AsyncIterable[object], handle)
        read = _MemoryRead(source, limits, {"provider": "memory"})
        try:
            yield read
        finally:
            await read.close()
