"""Incremental JSON-array and JSONL sources with physical row provenance."""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal

from mic.errors import ConfigurationError, DatasetError
from mic.models import JsonObject
from mic.sources import DatasetSource, ReadContext, RecordError

from ._io import parse_json


@dataclass(frozen=True)
class FileHandle(DatasetSource):
    path: Path | str
    format: Literal["json", "jsonl"] | None = None

    def read(self, ctx: ReadContext) -> Iterator[object]:
        path = Path(self.path).expanduser().resolve()
        format = self.format or (
            "jsonl" if path.suffix.lower() in {".jsonl", ".ndjson"} else "json"
        )
        if format not in {"json", "jsonl"}:
            raise ConfigurationError(f"unsupported file format {format!r}")
        ctx.set_provenance(provider="file", path=str(path), format=format)
        try:
            with ctx.open_binary(path) as stream:
                if format == "json":
                    for index, data in enumerate(_array_records(stream, ctx), start=1):
                        yield _record(data, path, index)
                else:
                    line_number = 0
                    index = 0
                    while data := stream.readline(ctx.limits.max_record_bytes + 2):
                        line_number += 1
                        payload = data.rstrip(b"\r\n")
                        ctx.check_record_bytes(len(payload))
                        if not payload.strip():
                            continue
                        index += 1
                        yield _record(payload, path, index, line=line_number)
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


def _array_records(stream: BinaryIO, ctx: ReadContext) -> Iterator[bytes]:
    """Frame one JSON value at a time, without decoding the surrounding array.

    Framing errors abort: without a reliable boundary it is unsafe to skip ahead.
    Individual framed values still pass through the strict JSON decoder.
    """
    started = ended = quoted = escaped = False
    depth = 0
    after_comma = False
    record = bytearray()
    while chunk := stream.read(min(8192, ctx.limits.max_record_bytes + 1)):
        for byte in chunk:
            if not started:
                if byte in b" \t\r\n":
                    continue
                if byte != ord("["):
                    raise DatasetError("JSON file must contain an array of case objects")
                started = True
                continue
            if ended:
                if byte not in b" \t\r\n":
                    raise DatasetError("Unexpected content after JSON array")
                continue
            if not quoted and depth == 0 and byte in b",]":
                payload = bytes(record).strip()
                if payload:
                    yield payload
                elif byte == ord(",") or after_comma:
                    raise DatasetError("Empty record or trailing comma in JSON array")
                record.clear()
                after_comma = byte == ord(",")
                ended = byte == ord("]")
                continue
            if quoted:
                if escaped:
                    escaped = False
                elif byte == ord("\\"):
                    escaped = True
                elif byte == ord('"'):
                    quoted = False
            elif byte == ord('"'):
                quoted = True
            elif byte in b"{[":
                depth += 1
            elif byte in b"}]":
                depth -= 1
                if depth < 0:
                    raise DatasetError("Unbalanced JSON array record")
            if record or byte not in b" \t\r\n":
                record.append(byte)
                ctx.check_record_bytes(len(record))
    if not ended or depth or quoted:
        raise DatasetError("Incomplete JSON array")
