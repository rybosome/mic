"""Small, dependency-free read boundaries shared by the adapters."""

import asyncio
import json
import math
from collections.abc import Callable, Iterator
from typing import cast

from mic.errors import DatasetError
from mic.models import JsonValue

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
                raise ValueError("duplicate object key")
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
