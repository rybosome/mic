"""Bounded JSON-array and JSONL file reads with physical row provenance."""

import hashlib
import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Literal

from mic.errors import ConfigurationError, DatasetError
from mic.models import JsonObject, JsonValue, ReadLimits

from ._io import ReadBudget, in_thread, parse_json


@dataclass(frozen=True)
class FileHandle:
    path: Path | str
    format: Literal["json", "jsonl"] | None = None
    provider: Literal["file"] = field(default="file", init=False)


@dataclass
class _FileRead:
    stream: BinaryIO
    path: Path
    format: Literal["json", "jsonl"]
    limits: ReadLimits
    provenance: JsonObject
    used: bool = False

    def _row(self, row: JsonValue, index: int, *, line: int | None = None) -> JsonObject:
        if not isinstance(row, dict):
            raise DatasetError(f"{self.path}: row {index}: expected a JSON case object")
        result = dict(row)
        existing = result.get("provenance", {})
        if not isinstance(existing, dict):
            raise DatasetError(f"{self.path}: row {index}: provenance must be an object")
        provenance: JsonObject = {**existing, "file": str(self.path), "source_row": index}
        if line is not None:
            provenance["line"] = line
        result["provenance"] = provenance
        return result

    async def rows(self) -> AsyncIterator[object]:
        if self.used:
            raise DatasetError(f"{self.path}: a read may only be iterated once")
        self.used = True
        budget = ReadBudget(self.limits, str(self.path))
        digest = hashlib.sha256()
        self.provenance["read_complete"] = False
        if self.format == "json":
            data = await in_thread(lambda: self.stream.read(self.limits.max_bytes + 1))
            budget.add_bytes(len(data))
            digest.update(data)
            self.provenance.update({"raw_sha256": digest.hexdigest(), "raw_bytes": budget.bytes})
            content = parse_json(data, str(self.path))
            if not isinstance(content, list):
                raise DatasetError(f"{self.path}: JSON file must contain an array of case objects")
            for index, row in enumerate(content, start=1):
                size = len(json.dumps(row, ensure_ascii=False, allow_nan=False).encode("utf-8"))
                budget.add_row(size)
                yield self._row(row, index)
        else:
            line_number = 0
            while True:
                data = await in_thread(
                    lambda: self.stream.readline(self.limits.max_record_bytes + 2)
                )
                if not data:
                    break
                line_number += 1
                digest.update(data)
                budget.add_bytes(len(data))
                payload = data.rstrip(b"\r\n")
                if len(payload) > self.limits.max_record_bytes:
                    raise DatasetError(
                        f"{self.path}: line {line_number}: "
                        f"max_record_bytes={self.limits.max_record_bytes} exceeded"
                    )
                if not payload.strip():
                    continue
                budget.add_row(len(payload))
                self.provenance.update(
                    {"raw_prefix_sha256": digest.hexdigest(), "raw_bytes": budget.bytes}
                )
                row = parse_json(payload, f"{self.path}: line {line_number}")
                yield self._row(row, budget.rows, line=line_number)
        self.provenance.pop("raw_prefix_sha256", None)
        self.provenance.update(
            {"raw_sha256": digest.hexdigest(), "raw_bytes": budget.bytes, "read_complete": True}
        )


class FileLoader:
    @asynccontextmanager
    async def open(self, handle: FileHandle, *, limits: ReadLimits) -> AsyncGenerator[_FileRead]:
        path = Path(handle.path).expanduser().resolve()
        format = handle.format
        if format is None:
            format = "jsonl" if path.suffix.lower() in {".jsonl", ".ndjson"} else "json"
        if format not in {"json", "jsonl"}:
            raise ConfigurationError(f"unsupported file format {format!r}")
        try:
            with path.open("rb") as stream:
                yield _FileRead(
                    stream,
                    path,
                    format,
                    limits,
                    {"provider": "file", "path": str(path), "format": format},
                )
        except OSError as exc:
            raise DatasetError(f"{path}: could not read dataset: {exc}") from exc
