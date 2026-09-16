"""Explicit handle-to-loader dispatch. The runner knows no provider types."""

from collections.abc import AsyncIterable, Awaitable, Callable, Iterable
from contextlib import AbstractAsyncContextManager
from typing import cast

from .errors import ConfigurationError
from .models import DatasetLoader, DatasetRead, JsonObject, ReadLimits
from .validation import json_object

type OpenSource = Callable[[object, ReadLimits], AbstractAsyncContextManager[DatasetRead]]


class Resolver:
    def __init__(self) -> None:
        self._loaders: dict[type[object], OpenSource] = {}
        self._estimates: dict[type[object], Callable[[object], Awaitable[JsonObject]]] = {}

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
            from .providers.memory import MemoryLoader

            return MemoryLoader().open(
                cast(Iterable[object] | AsyncIterable[object], source), limits=limits
            )
        raise ConfigurationError(f"No dataset loader registered for {type(source).__name__}")


def default_resolver() -> Resolver:
    # These modules must not import provider SDKs at module scope.
    from .providers.bigquery import BigQueryHandle, BigQueryLoader
    from .providers.braintrust import BraintrustHandle, BraintrustLoader
    from .providers.files import FileHandle, FileLoader

    resolver = Resolver()
    resolver.register(FileHandle, FileLoader())
    resolver.register(BigQueryHandle, BigQueryLoader())
    resolver.register(BraintrustHandle, BraintrustLoader())
    return resolver
