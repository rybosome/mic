"""Public contracts for custom dataset providers and resolver composition."""

from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Iterable
from contextlib import AbstractAsyncContextManager
from typing import Protocol, cast

from .._runtime.validation import json_object
from ..errors import ConfigurationError
from ..models import JsonObject, ReadLimits


class DatasetRead(Protocol):
    @property
    def provenance(self) -> JsonObject: ...

    def rows(self) -> AsyncIterator[object]: ...


class DatasetLoader[H](Protocol):
    def open(
        self, handle: H, *, limits: ReadLimits
    ) -> AbstractAsyncContextManager[DatasetRead]: ...


type _OpenSource = Callable[[object, ReadLimits], AbstractAsyncContextManager[DatasetRead]]


class Resolver:
    def __init__(self) -> None:
        self._loaders: dict[type[object], _OpenSource] = {}
        self._estimates: dict[type[object], Callable[[object], Awaitable[JsonObject]]] = {}

    @classmethod
    def with_builtin_loaders(cls) -> "Resolver":
        """Create a resolver with Mic's file and optional cloud handles registered."""
        from .bigquery import BigQueryHandle, BigQueryLoader
        from .braintrust import BraintrustHandle, BraintrustLoader
        from .files import FileHandle, FileLoader

        resolver = cls()
        resolver.register(FileHandle, FileLoader())
        resolver.register(BigQueryHandle, BigQueryLoader())
        resolver.register(BraintrustHandle, BraintrustLoader())
        return resolver

    def register[H](self, handle_type: type[H], loader: DatasetLoader[H]) -> None:
        if handle_type in self._loaders:
            raise ConfigurationError(f"Loader already registered for {handle_type.__name__}")

        def open_source(
            source: object, limits: ReadLimits
        ) -> AbstractAsyncContextManager[DatasetRead]:
            if not isinstance(source, handle_type):
                raise ConfigurationError(
                    f"Expected {handle_type.__name__}, got {type(source).__name__}"
                )
            return loader.open(source, limits=limits)

        self._loaders[handle_type] = open_source
        estimator = getattr(loader, "estimate", None)
        if callable(estimator):
            estimate_handle = cast(Callable[[H], Awaitable[JsonObject]], estimator)

            async def estimate_source(source: object) -> JsonObject:
                if not isinstance(source, handle_type):
                    raise ConfigurationError(f"Expected {handle_type.__name__}")
                return json_object(await estimate_handle(source))

            self._estimates[handle_type] = estimate_source

    async def estimate(self, source: object) -> JsonObject:
        estimate = self._estimates.get(type(source))
        if estimate is None:
            raise ConfigurationError(f"{type(source).__name__} does not support cost estimation")
        return await estimate(source)

    def loader[H](
        self, handle_type: type[H]
    ) -> Callable[[type[DatasetLoader[H]]], type[DatasetLoader[H]]]:
        def decorate(loader_type: type[DatasetLoader[H]]) -> type[DatasetLoader[H]]:
            self.register(handle_type, loader_type())
            return loader_type

        return decorate

    def open(
        self, source: object, *, limits: ReadLimits
    ) -> AbstractAsyncContextManager[DatasetRead]:
        registered = self._loaders.get(type(source))
        if registered is not None:
            return registered(source, limits)
        if isinstance(source, (str, bytes, dict)):
            raise ConfigurationError(
                "A dataset source must be a handle or iterable of rows, not a string/bytes/dict"
            )
        if isinstance(source, (Iterable, AsyncIterable)):
            from .memory import MemoryLoader

            return MemoryLoader().open(
                cast(Iterable[object] | AsyncIterable[object], source), limits=limits
            )
        raise ConfigurationError(f"No dataset loader registered for {type(source).__name__}")
