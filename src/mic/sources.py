"""Public source API shared by built-in providers and external extensions."""

import hashlib
import io
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast

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
    """Per-read limits and bounded provenance, independent of task/output types.

    Providers account physical bytes with ``account_bytes`` and check decoded
    record sizes with ``check_record_bytes``. Mic separately bounds normalized
    records; neither count claims to measure HTTP wire traffic.
    """

    def __init__(self, limits: ReadLimits) -> None:
        self.limits = limits
        self.rows_seen = 0
        self.raw_bytes = 0
        self._provenance: JsonObject = {}

    @property
    def provenance(self) -> JsonObject:
        return json_object(self._provenance)

    def set_provenance(self, **values: JsonValue) -> None:
        updated = json_object({**self._provenance, **values})
        if len(dumps(updated).encode("utf-8")) > 65_536:
            raise DatasetError("Source provenance exceeds 65536 bytes")
        self._provenance = updated

    def account_bytes(self, size: int) -> None:
        if type(size) is not int or size < 0:
            raise ValueError("Byte counts must be nonnegative integers")
        self.raw_bytes += size
        if self.raw_bytes > self.limits.max_bytes:
            raise DatasetError(f"Source exceeds max_bytes={self.limits.max_bytes}")

    def check_record_bytes(self, size: int) -> None:
        if type(size) is not int or size < 0:
            raise ValueError("Byte counts must be nonnegative integers")
        if size > self.limits.max_record_bytes:
            raise DatasetError(f"Record exceeds max_record_bytes={self.limits.max_record_bytes}")

    @contextmanager
    def open_binary(self, path: Path | str) -> Generator[BinaryIO]:
        """Open an owned budgeted file; digest covers only bytes actually read."""
        with Path(path).expanduser().open("rb") as stream:
            with _BudgetedReader(stream, self) as bounded:
                yield cast(BinaryIO, bounded)


class _BudgetedReader(io.BufferedIOBase):
    def __init__(self, stream: BinaryIO, ctx: ReadContext) -> None:
        self._stream = stream
        self._ctx = ctx
        self._digest = hashlib.sha256()
        self._bytes = 0
        self._complete = False

    def readable(self) -> bool:
        return True

    def _size(self, size: int | None) -> int:
        remaining = self._ctx.limits.max_bytes - self._ctx.raw_bytes + 1
        return remaining if size is None or size < 0 else min(size, remaining)

    def _received(self, data: bytes, requested: int) -> bytes:
        self._ctx.account_bytes(len(data))
        self._bytes += len(data)
        self._digest.update(data)
        if requested and not data:
            self._complete = True
        self._ctx.set_provenance(
            raw_bytes=self._bytes,
            raw_prefix_sha256=self._digest.hexdigest(),
            read_complete=self._complete,
        )
        if self._complete:
            self._ctx.set_provenance(raw_sha256=self._digest.hexdigest())
        return data

    def read(self, size: int | None = -1) -> bytes:
        requested = self._size(size)
        return self._received(self._stream.read(requested), requested)

    def read1(self, size: int = -1) -> bytes:
        return self.read(size)

    def readline(self, size: int | None = -1) -> bytes:
        requested = self._size(size)
        return self._received(self._stream.readline(requested), requested)
