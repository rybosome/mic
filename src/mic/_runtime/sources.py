"""One lazy, cancellation-safe source driver; no provider registry or dispatch."""

import asyncio
from collections.abc import (
    AsyncGenerator,
    AsyncIterable,
    AsyncIterator,
    Callable,
    Iterable,
    Iterator,
)
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Protocol, cast, runtime_checkable

from .._async import drain
from ..errors import ConfigurationError, DatasetError
from ..models import JsonObject, ReadLimits
from ..sources import DatasetSource, ReadContext


@runtime_checkable
class _Close(Protocol):
    def close(self) -> None: ...


@runtime_checkable
class _AsyncClose(Protocol):
    async def aclose(self) -> None: ...


def _next(iterator: Iterator[object]) -> tuple[bool, object]:
    try:
        return True, next(iterator)
    except StopIteration:
        return False, None


def _checked_iterator(value: object) -> Iterator[object] | AsyncIterator[object]:
    if not isinstance(value, (Iterator, AsyncIterator)):
        raise ConfigurationError("DatasetSource.read must return an iterator")
    return cast(Iterator[object] | AsyncIterator[object], value)


class SourceRead:
    def __init__(self, source: object, limits: ReadLimits) -> None:
        self.context = ReadContext(limits)
        self._source = source
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mic-source")
        self._iterator: Iterator[object] | AsyncIterator[object] | None = None
        self._used = False

    @property
    def provenance(self) -> JsonObject:
        return self.context.provenance

    async def _call[T](self, function: Callable[[], T]) -> T:
        pending = asyncio.get_running_loop().run_in_executor(self._worker, function)
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            # Resource ownership cannot end while a worker still uses it.
            try:
                await drain(pending)
            except Exception:
                pass
            raise

    def _initialize(self) -> None:
        source = self._source
        if isinstance(source, DatasetSource):
            self._iterator = _checked_iterator(source.read(self.context))
        elif isinstance(source, AsyncIterable):
            self._iterator = aiter(cast(AsyncIterable[object], source))
        elif not isinstance(source, (str, bytes, dict)) and isinstance(source, Iterable):
            self._iterator = iter(cast(Iterable[object], source))
        else:
            raise ConfigurationError("Dataset source must be a DatasetSource or iterable of rows")

    async def rows(self) -> AsyncGenerator[object]:
        if self._used:
            raise DatasetError("A read may only be iterated once; reopen the source")
        self._used = True
        await self._call(self._initialize)
        iterator = self._iterator
        assert iterator is not None
        while True:
            if isinstance(iterator, AsyncIterator):
                try:
                    row = await anext(iterator)
                except StopAsyncIteration:
                    break
            else:
                present, row = await self._call(lambda: _next(iterator))
                if not present:
                    break
            self.context.rows_seen += 1
            if self.context.rows_seen > self.context.limits.max_rows:
                raise DatasetError(f"Source exceeds max_rows={self.context.limits.max_rows}")
            yield row

    async def close(self) -> None:
        try:
            if isinstance(self._iterator, _AsyncClose):
                await drain(asyncio.create_task(self._iterator.aclose()))
            elif isinstance(self._iterator, _Close):
                await drain(asyncio.create_task(self._call(self._iterator.close)))
        finally:
            # Every submitted operation has been joined before reaching here.
            self._worker.shutdown(wait=True)


@asynccontextmanager
async def open_source(source: object, *, limits: ReadLimits) -> AsyncGenerator[SourceRead]:
    read = SourceRead(source, limits)
    try:
        yield read
    finally:
        await read.close()
