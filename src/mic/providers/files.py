"""Incremental JSON-array and JSONL sources with physical row provenance."""

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, cast

from mic.errors import ConfigurationError, DatasetError
from mic.models import JsonObject
from mic.sources import DatasetSource, ReadContext, RecordError

from ._io import parse_json
from ._json_stream import array_records


@dataclass(frozen=True)
class JSONLFileHandle(DatasetSource):
    """Read one JSON object per nonblank physical line, regardless of suffix."""

    path: Path | str

    def read(self, ctx: ReadContext) -> Iterator[object]:
        path = Path(self.path).expanduser().resolve()
        ctx.set_provenance(provider="file", path=str(path), format="jsonl")
        try:
            with path.open("rb") as raw:
                stream = _FileReader(raw, ctx)
                line_number = 0
                index = 0
                while data := stream.readline():
                    line_number += 1
                    payload = data.rstrip(b"\r\n")
                    if not payload.strip():
                        continue
                    index += 1
                    yield _record(payload, path, index, line=line_number)
        except OSError:
            raise DatasetError("Could not read dataset file") from None


@dataclass(frozen=True)
class JSONFileHandle(DatasetSource):
    """Read an array of objects, optionally under an exact top-level object key."""

    path: Path | str
    records_key: str | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        if self.records_key is not None and not isinstance(cast(object, self.records_key), str):
            raise ConfigurationError("records_key must be a string or None")

    def read(self, ctx: ReadContext) -> Iterator[object]:
        path = Path(self.path).expanduser().resolve()
        ctx.set_provenance(provider="file", path=str(path), format="json")
        if self.records_key is not None:
            ctx.set_provenance(records_key=self.records_key)
        try:
            with path.open("rb") as raw:
                stream = _FileReader(raw, ctx)
                for index, data in enumerate(array_records(stream, self.records_key), start=1):
                    yield _record(data, path, index)
        except OSError:
            raise DatasetError("Could not read dataset file") from None


def _record(data: bytes, path: Path, index: int, *, line: int | None = None) -> object:
    source = f"{path}: " + (f"line {line}" if line is not None else f"row {index}")
    try:
        row = parse_json(data, source)
        if not isinstance(row, dict):
            raise DatasetError(f"{source}: expected a JSON case object")
        existing = row.get("provenance", {})
        if not isinstance(existing, dict):
            raise DatasetError(f"{source}: provenance must be an object")
        provenance: JsonObject = {**existing, "file": str(path), "source_row": index}
        if line is not None:
            provenance["line"] = line
        return {**row, "provenance": provenance}
    except DatasetError as exc:
        return RecordError(str(exc))


class _FileReader:
    """Track consumed file bytes and their digest without limiting input size."""

    def __init__(self, stream: BinaryIO, ctx: ReadContext) -> None:
        self._stream = stream
        self._ctx = ctx
        self._digest = hashlib.sha256()
        self._bytes = 0

    def _received(self, data: bytes) -> bytes:
        self._bytes += len(data)
        self._digest.update(data)
        self._ctx.set_provenance(
            raw_bytes=self._bytes,
            raw_prefix_sha256=self._digest.hexdigest(),
            read_complete=not data,
        )
        if not data:
            self._ctx.set_provenance(raw_sha256=self._digest.hexdigest())
        return data

    def read(self, size: int) -> bytes:
        return self._received(self._stream.read(size))

    def readline(self) -> bytes:
        return self._received(self._stream.readline())
