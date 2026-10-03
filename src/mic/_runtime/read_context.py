"""Runtime-owned read accounting and passive factory signature validation."""

import inspect
import time
from collections.abc import Callable
from typing import cast

from ..errors import ConfigurationError
from ..sources import ReadContext


class ReadState(ReadContext):
    """Only the runtime advances the public read context."""

    def start(self) -> None:
        self._started = time.perf_counter()

    def pause(self) -> None:
        self._remaining = self.remaining_seconds
        self._started = None

    def consumed(self) -> None:
        self._rows_seen += 1


def factory_call(factory: object, ctx: ReadContext) -> Callable[[], object]:
    """Validate without invoking or retrying user code."""
    if not callable(factory):
        raise ConfigurationError("Dataset factory must be callable")
    try:
        parameters = tuple(inspect.signature(factory).parameters.values())
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("Dataset factory must have an inspectable signature") from exc
    positional = [p for p in parameters if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    if len(positional) > 1 or any(
        p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
        or (p.kind == p.KEYWORD_ONLY and p.default is p.empty)
        for p in parameters
    ):
        raise ConfigurationError("Dataset factory must accept () or (context)")
    if positional:
        return lambda: cast(Callable[[ReadContext], object], factory)(ctx)
    return cast(Callable[[], object], factory)
