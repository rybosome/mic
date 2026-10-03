"""Row mapping, bounded snapshots and provider-independent inspection."""

import asyncio
import hashlib
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import cast

from .._async import drain, run_sync
from ..errors import ConfigurationError, DatasetError
from ..models import MISSING, Dataset, JsonObject, JsonValue, Missing, RawCase, ReadLimits
from ..sources import RecordError
from .contracts import Case
from .sources import open_source
from .validation import (
    describe,
    dumps,
    json_object,
    nonempty,
    positive_integer,
    serialize,
    validate,
)


def map_envelope(row: object) -> RawCase:
    if isinstance(row, RawCase):
        return row
    if not isinstance(row, Mapping):
        raise TypeError("A dataset row must be a mapping or RawCase")
    value = cast(Mapping[str, object], row)
    if "input" not in value:
        raise ValueError("Missing required field 'input'")
    row_id = value.get("id")
    if row_id is not None and (not isinstance(row_id, str) or not row_id.strip()):
        raise ValueError("Case id must be a non-empty string")
    return RawCase(
        input=value["input"],
        id=row_id,
        expected=value.get("expected", MISSING),
        metadata=value.get("metadata"),
        provenance=json_object(value.get("provenance", {})),
    )


@dataclass(frozen=True)
class DatasetSnapshot[I, E, M]:
    cases: list[Case[I, E, M]]
    rows: list[JsonObject]
    summary: JsonObject


async def resolve_source[I, E, M](dataset: Dataset[I, E, M]) -> object:
    factory = dataset.factory
    if inspect.iscoroutinefunction(factory):
        return await cast(Awaitable[object], factory())
    pending = asyncio.create_task(asyncio.to_thread(factory))
    try:
        result = await asyncio.shield(pending)
    except asyncio.CancelledError:
        try:
            abandoned = await drain(pending)
            if inspect.iscoroutine(abandoned):
                abandoned.close()
            else:
                async_close = getattr(abandoned, "aclose", None)
                close = getattr(abandoned, "close", None)
                if callable(async_close):
                    await drain(
                        asyncio.ensure_future(cast(Callable[[], Awaitable[None]], async_close)())
                    )
                elif callable(close):
                    await drain(
                        asyncio.create_task(asyncio.to_thread(cast(Callable[[], None], close)))
                    )
        except Exception:
            pass
        raise
    return await cast(Awaitable[object], result) if inspect.isawaitable(result) else result


async def load_dataset[I, E, M](
    dataset: Dataset[I, E, M],
    *,
    limits: ReadLimits | None = None,
    limit: int | None = None,
) -> DatasetSnapshot[I, E, M]:
    nonempty("dataset name", dataset.name)
    if dataset.schema.expected_policy not in ("required", "optional"):
        raise ConfigurationError("expected_policy must be 'required' or 'optional'")
    if limit is not None:
        positive_integer("limit", limit)
    caps = limits or ReadLimits()
    cases: list[Case[I, E, M]] = []
    rows: list[JsonObject] = []
    ids: set[str] = set()
    byte_count = 0
    source_info: JsonObject = {}
    schema = dataset.schema
    schema_json: JsonObject = {
        "input": describe(schema.input, "input"),
        "expected": describe(schema.expected, "expected"),
        "metadata": describe(schema.metadata, "metadata"),
        "expected_policy": schema.expected_policy,
    }
    row_index = 0
    try:
        async with asyncio.timeout(caps.timeout_seconds):
            source = await resolve_source(dataset)
            async with open_source(source, limits=caps) as read:
                source_info = json_object(read.provenance)
                iterator = read.rows()
                try:
                    async for raw in iterator:
                        if isinstance(raw, RecordError):
                            raise DatasetError(raw.message)
                        if row_index >= caps.max_rows:
                            raise DatasetError(
                                f"Dataset exceeds max_rows={caps.max_rows}; no tasks started"
                            )
                        mapped = _mapped_case(dataset.map_row(raw))
                        case_input = validate(schema.input, mapped.input)
                        if isinstance(mapped.expected, Missing):
                            if schema.expected_policy == "required":
                                raise ValueError("Missing required field 'expected'")
                            expected: E | Missing = mapped.expected
                        else:
                            expected = validate(schema.expected, mapped.expected)
                        metadata = (
                            None
                            if mapped.metadata is None
                            else validate(schema.metadata, mapped.metadata)
                        )
                        normalized: JsonObject = {"input": serialize(schema.input, case_input)}
                        if not isinstance(expected, Missing):
                            normalized["expected"] = serialize(schema.expected, expected)
                        if metadata is not None:
                            normalized["metadata"] = json_object(
                                serialize(schema.metadata, metadata), "$.metadata"
                            )
                        if mapped.id is not None:
                            row_id = nonempty("case id", mapped.id)
                        else:
                            # Logical identity must not depend on the source provider.
                            basis = hashlib.sha256(dumps(normalized).encode()).hexdigest()
                            row_id = f"{basis[:16]}:{row_index + 1}"
                        if row_id in ids:
                            raise ValueError(f"Duplicate case id {row_id!r}")
                        ids.add(row_id)
                        normalized["id"] = row_id
                        size = len(dumps(normalized).encode("utf-8")) + 1
                        if size > caps.max_record_bytes:
                            raise DatasetError(
                                f"Case {row_id!r} exceeds max_record_bytes={caps.max_record_bytes}"
                            )
                        byte_count += size
                        if byte_count > caps.max_bytes:
                            raise DatasetError(
                                f"Dataset exceeds max_bytes={caps.max_bytes}; no tasks started"
                            )
                        provenance = json_object(mapped.provenance)
                        cases.append(Case(row_id, case_input, expected, metadata, provenance))
                        rows.append(normalized)
                        row_index += 1
                        if limit is not None and row_index >= limit:
                            break
                finally:
                    close = getattr(iterator, "aclose", None)
                    if callable(close):
                        await cast(Callable[[], Awaitable[None]], close)()
                # Query IDs and row counts may resolve only after reading finishes.
                source_info = json_object(read.provenance)
    except ConfigurationError:
        raise
    except Exception as exc:
        if isinstance(exc, DatasetError):
            raise
        raise DatasetError(
            f"Dataset {dataset.name!r}, row {row_index + 1}: {type(exc).__name__}: {exc}"
        ) from exc
    logical_bytes = "".join(dumps(row) + "\n" for row in rows).encode("utf-8")
    summary: JsonObject = {
        "name": dataset.name,
        "rows": len(rows),
        "bytes": byte_count,
        "digest": "sha256:" + hashlib.sha256(logical_bytes).hexdigest(),
        "provenance": source_info,
        "schema": schema_json,
        "selection": {"limit": limit},
    }
    return DatasetSnapshot(cases, rows, summary)


def _mapped_case(value: object) -> RawCase:
    if not isinstance(value, RawCase):
        raise TypeError("map_row must return RawCase")
    return value


async def ainspect_dataset[I, E, M](
    dataset: Dataset[I, E, M],
    *,
    limit: int | None = None,
    limits: ReadLimits | None = None,
) -> JsonObject:
    snapshot = await load_dataset(dataset, limits=limits, limit=limit)
    return {"dataset": snapshot.summary, "rows": cast(list[JsonValue], snapshot.rows)}


def inspect_dataset[I, E, M](
    dataset: Dataset[I, E, M],
    *,
    limit: int | None = None,
    limits: ReadLimits | None = None,
) -> JsonObject:
    return run_sync(
        "mic.inspect_dataset()",
        "mic.ainspect_dataset",
        lambda: ainspect_dataset(dataset, limit=limit, limits=limits),
    )
