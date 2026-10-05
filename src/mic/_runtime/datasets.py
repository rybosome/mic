"""Map and validate a single record at a time; retain only read summaries."""

import asyncio
import hashlib
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from typing import Literal, cast

from .._async import run_sync
from ..errors import ConfigurationError, DatasetError
from ..models import MISSING, Dataset, JsonObject, JsonValue, Missing, RawCase, ReadLimits
from ..results import Failure, SourceSummary, failure, to_json
from ..sources import RecordError
from .callbacks import CallbackPool
from .contracts import Case
from .read_context import ReadState
from .sources import SourceRead
from .validation import (
    describe,
    dumps,
    json_object,
    nonempty,
    serialize,
    validate,
)


def map_envelope(row: object) -> RawCase:
    if isinstance(row, RawCase):
        return row
    if isinstance(row, tuple):
        pair = cast(tuple[object, ...], row)
        if len(pair) != 2:
            raise ValueError("A tuple dataset row must contain exactly (input, expected)")
        return RawCase(input=pair[0], expected=pair[1])
    if not isinstance(row, Mapping):
        raise TypeError("A dataset row must be a mapping, RawCase, or (input, expected) tuple")
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


def describe_dataset[I, E, M](dataset: Dataset[I, E, M]) -> JsonObject:
    nonempty("dataset name", dataset.name)
    if dataset.schema.expected_policy not in ("required", "optional"):
        raise ConfigurationError("expected_policy must be 'required' or 'optional'")
    return {
        "name": dataset.name,
        "input": describe(dataset.schema.input, "input"),
        "expected": describe(dataset.schema.expected, "expected"),
        "metadata": describe(dataset.schema.metadata, "metadata"),
        "expected_policy": dataset.schema.expected_policy,
    }


def _raw_case(value: object) -> RawCase:
    if not isinstance(value, RawCase):
        raise TypeError("map_row must return RawCase")
    return value


def normalize_case[I, E, M](
    dataset: Dataset[I, E, M],
    raw: object,
    case_id: str,
    require_expected: bool,
) -> tuple[Case[I, E, M], JsonObject]:
    mapped = _raw_case(dataset.map_row(raw))
    schema = dataset.schema
    value = validate(schema.input, mapped.input)
    if isinstance(mapped.expected, Missing):
        if schema.expected_policy == "required" or require_expected:
            raise ValueError("This dataset or its scorers require expected")
        expected: E | Missing = mapped.expected
    else:
        expected = validate(schema.expected, mapped.expected)
    metadata = None if mapped.metadata is None else validate(schema.metadata, mapped.metadata)
    normalized: JsonObject = {"input": serialize(schema.input, value)}
    if not isinstance(expected, Missing):
        normalized["expected"] = serialize(schema.expected, expected)
    if metadata is not None:
        normalized["metadata"] = json_object(serialize(schema.metadata, metadata))
    if mapped.id is not None:
        normalized["label"] = nonempty("case label", mapped.id)
    provenance = json_object(mapped.provenance)
    if provenance:
        normalized["provenance"] = provenance
    return Case(case_id, value, expected, metadata, provenance), normalized


class DatasetReader[I, E, M]:
    def __init__(
        self,
        dataset: Dataset[I, E, M],
        source_id: str,
        limits: ReadLimits,
        *,
        on_invalid: Literal["abort", "skip"] = "abort",
        require_expected: bool = False,
    ) -> None:
        self.dataset = dataset
        self.source_id = source_id
        self.limits = limits
        self.on_invalid = on_invalid
        self.require_expected = require_expected
        self.seen = self.accepted = self.rejected = 0
        self.exhausted = False
        self.error: Failure | None = None
        self.provenance: JsonObject = {}
        self._digest = hashlib.sha256()
        self._used = False
        self.context = ReadState(limits)

    def summary(self) -> SourceSummary:
        return SourceSummary(
            self.dataset.name,
            self.seen,
            self.accepted,
            self.rejected,
            self.exhausted,
            "sha256:" + self._digest.hexdigest(),
            json_object(self.provenance),
            self.error,
        )

    async def _timed[T](self, function: Callable[[], Awaitable[T]]) -> T:
        self.context.start()
        try:
            remaining = self.context.remaining_seconds
            if remaining is not None and remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(remaining):
                result = await function()
                if self.context.remaining_seconds == 0:
                    raise TimeoutError
                return result
        except TimeoutError:
            raise DatasetError("Source read budget exceeded (TimeoutError)") from None
        finally:
            self.context.pause()

    async def rows(
        self,
    ) -> AsyncGenerator[tuple[int, Case[I, E, M], JsonObject] | tuple[int, Failure]]:
        if self._used:
            raise ConfigurationError("A dataset read may only be consumed once")
        self._used = True
        pool = CallbackPool(1)
        read = SourceRead(None, self.limits, context=self.context)
        try:
            await self._timed(lambda: read.resolve(self.dataset.factory))
            iterator = read.rows()
            try:
                while True:
                    try:
                        raw = await self._timed(lambda: anext(iterator))
                    except StopAsyncIteration:
                        self.exhausted = read.exhausted
                        break
                    index = self.seen
                    self.seen += 1
                    case_id = f"{self.source_id}:r{index}"
                    try:
                        if isinstance(raw, RecordError):
                            raise ValueError("Source yielded a malformed record")
                        case, normalized = await self._timed(
                            lambda: pool.invoke(
                                normalize_case,
                                self.dataset,
                                raw,
                                case_id,
                                self.require_expected,
                            )
                        )
                    except (ValueError, TypeError) as exc:
                        self.rejected += 1
                        error = failure("record", exc)
                        yield index, error
                        if self.on_invalid == "abort":
                            raise DatasetError("Malformed dataset record") from None
                        continue
                    logical = {
                        key: value for key, value in normalized.items() if key != "provenance"
                    }
                    self._digest.update((dumps(logical) + "\n").encode("utf-8"))
                    self.accepted += 1
                    yield index, case, normalized
            finally:
                await iterator.aclose()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = failure("source", exc)
            raise
        finally:
            try:
                await read.close()
            except Exception as exc:
                if self.error is None:
                    self.error = failure("source", exc)
                raise
            finally:
                self.provenance = read.provenance
                self.seen = read.context.rows_seen
                await pool.close()


async def ainspect_dataset[I, E, M](
    dataset: Dataset[I, E, M],
    *,
    limits: ReadLimits | None = None,
) -> JsonObject:
    describe_dataset(dataset)
    configured = limits or ReadLimits()
    effective = ReadLimits(
        row_count=configured.row_count or 20, timeout_seconds=configured.timeout_seconds
    )
    reader = DatasetReader(dataset, "inspection", effective)
    rows: list[JsonValue] = []
    iterator = reader.rows()
    try:
        async for item in iterator:
            if len(item) == 2:
                raise DatasetError("Malformed dataset record")
            index, case, value = item
            rows.append({"case_id": case.id, "row_index": index, **value})
    except (ConfigurationError, DatasetError):
        raise
    except Exception as exc:
        raise DatasetError(f"Dataset inspection failed ({type(exc).__name__})") from None
    finally:
        await iterator.aclose()
    return {"dataset": to_json(reader.summary()), "rows": rows}


def inspect_dataset[I, E, M](
    dataset: Dataset[I, E, M],
    *,
    limits: ReadLimits | None = None,
) -> JsonObject:
    return run_sync(
        "mic.inspect_dataset()",
        "mic.ainspect_dataset",
        lambda: ainspect_dataset(dataset, limits=limits),
    )
