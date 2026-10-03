"""Public source API shared by built-in providers and external extensions."""

import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

from ._runtime.validation import dumps, json_object
from .errors import DatasetError
from .models import JsonObject, JsonValue, ReadLimits


@dataclass(frozen=True)
class RecordError:
    """A recoverable malformed record; message must not contain record contents."""

    message: str


class DatasetSource(ABC):
    """Passive configuration which creates a resource-owning iterator per read."""

    @abstractmethod
    def read(self, ctx: "ReadContext") -> Iterator[object] | AsyncIterator[object]:
        """Yield raw records, owning resources until exhaustion or iterator close."""
        raise NotImplementedError


class ReadContext:
    """Per-read limits and bounded provenance, independent of task/output types."""

    def __init__(self, limits: ReadLimits) -> None:
        self._limits = limits
        self._rows_seen = 0
        self._remaining = limits.timeout_seconds
        self._started: float | None = None
        self._provenance: JsonObject = {}

    @property
    def limits(self) -> ReadLimits:
        return self._limits

    @property
    def rows_seen(self) -> int:
        """Raw records consumed before mapping, including rejected records."""
        return self._rows_seen

    @property
    def remaining_rows(self) -> int | None:
        limit = self.limits.row_count
        return None if limit is None else max(0, limit - self.rows_seen)

    @property
    def remaining_seconds(self) -> float | None:
        """Active read budget; downstream backpressure does not consume it."""
        if self._remaining is None:
            return None
        elapsed = 0.0 if self._started is None else time.perf_counter() - self._started
        return max(0.0, self._remaining - elapsed)

    @property
    def provenance(self) -> JsonObject:
        return json_object(self._provenance)

    def set_provenance(self, **values: JsonValue) -> None:
        updated = json_object({**self._provenance, **values})
        if len(dumps(updated).encode("utf-8")) > 65_536:
            raise DatasetError("Source provenance exceeds 65536 bytes")
        self._provenance = updated
