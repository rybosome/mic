"""Small, dependency-free read boundaries shared by the adapters."""

import asyncio
import json
import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import cast

from mic.errors import DatasetError
from mic.models import JsonValue, ReadLimits

from .._async import drain


def parse_json(data: bytes, source: str) -> JsonValue:
    def reject_constant(value: str) -> object:
        raise ValueError(f"nonfinite number {value}")

    def finite_float(value: str) -> float:
        result = float(value)
        if not math.isfinite(result):
            raise ValueError(f"nonfinite number {value}")
        return result

    def unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate object key {key!r}")
            result[key] = value
        return result

    try:
        return cast(
            JsonValue,
            json.loads(
                data,
                parse_constant=reject_constant,
                parse_float=finite_float,
                object_pairs_hook=unique_object,
            ),
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise DatasetError(f"{source}: invalid JSON: {exc}") from exc


@dataclass
class ReadBudget:
    limits: ReadLimits
    source: str
    rows: int = 0
    bytes: int = 0

    def add_bytes(self, size: int) -> None:
        self.bytes += size
        if self.bytes > self.limits.max_bytes:
            raise DatasetError(f"{self.source}: max_bytes={self.limits.max_bytes} exceeded")

    def add_row(self, size: int | None = None) -> None:
        self.rows += 1
        if self.rows > self.limits.max_rows:
            raise DatasetError(f"{self.source}: max_rows={self.limits.max_rows} exceeded")
        if size is not None and size > self.limits.max_record_bytes:
            raise DatasetError(
                f"{self.source}: row {self.rows}: "
                f"max_record_bytes={self.limits.max_record_bytes} exceeded"
            )


def next_item[T](iterator: Iterator[T]) -> tuple[bool, T | None]:
    # StopIteration cannot be raised through an asyncio Future.
    try:
        return True, next(iterator)
    except StopIteration:
        return False, None


async def in_thread[T](
    function: Callable[[], T], *, on_cancel: Callable[[T], object] | None = None
) -> T:
    """Join a cancelled read before its context manager closes the resource.

    Python cannot forcibly terminate synchronous SDK calls. Adapters use bounded
    request deadlines; cancellation waits for that in-flight operation to finish.
    """
    task = asyncio.create_task(asyncio.to_thread(function))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            result = await drain(task)
            if on_cancel is not None:
                cleanup = asyncio.create_task(asyncio.to_thread(on_cancel, result))
                await drain(cleanup)
        except Exception:
            pass
        raise
