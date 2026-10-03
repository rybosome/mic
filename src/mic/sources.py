"""Public source API shared by built-in providers and external extensions."""

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
        self.limits = limits
        self.rows_seen = 0
        self._provenance: JsonObject = {}

    @property
    def provenance(self) -> JsonObject:
        return json_object(self._provenance)

    def set_provenance(self, **values: JsonValue) -> None:
        updated = json_object({**self._provenance, **values})
        if len(dumps(updated).encode("utf-8")) > 65_536:
            raise DatasetError("Source provenance exceeds 65536 bytes")
        self._provenance = updated
