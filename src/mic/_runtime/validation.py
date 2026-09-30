"""Strict values at persisted-data and score boundaries."""

import dataclasses
import json
import math
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import cast

from ..errors import ConfigurationError
from ..models import JsonObject, JsonValue, Score
from ..schema import Schema


def positive_integer(name: str, value: int) -> int:
    if type(value) is not int or value <= 0:
        raise ConfigurationError(f"{name} must be a positive integer")
    return value


def nonempty(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{name} must be a non-empty string")
    return value.strip()


def reject_nonfinite(value: object, path: str = "$", seen: set[int] | None = None) -> None:
    """Inspect native typed values before serializers can replace NaN with null."""
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path}: nonfinite number is not supported")
    if isinstance(value, Decimal) and not value.is_finite():
        raise ValueError(f"{path}: nonfinite decimal is not supported")
    if value is None or isinstance(value, (str, int, bool, float, Decimal)):
        return
    visited: set[int] = seen if seen is not None else set()
    if id(value) in visited:
        return
    visited.add(id(value))
    if isinstance(value, Mapping):
        for key, child in cast(Mapping[object, object], value).items():
            reject_nonfinite(child, f"{path}.{key}", visited)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for index, child in enumerate(cast(Sequence[object], value)):
            reject_nonfinite(child, f"{path}[{index}]", visited)
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        for item in dataclasses.fields(value):
            reject_nonfinite(getattr(value, item.name), f"{path}.{item.name}", visited)


def json_value(value: object, path: str = "$") -> JsonValue:
    if value is None or type(value) in (str, bool, int):
        return cast(JsonValue, value)
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{path}: nonfinite number is not supported")
        return value
    if isinstance(value, Mapping):
        out: JsonObject = {}
        for key, child in cast(Mapping[object, object], value).items():
            if not isinstance(key, str):
                raise TypeError(f"{path}: JSON object keys must be strings")
            out[key] = json_value(child, f"{path}.{key}")
        return out
    if isinstance(value, list):
        return [
            json_value(child, f"{path}[{i}]") for i, child in enumerate(cast(list[object], value))
        ]
    raise TypeError(f"{path}: {type(value).__name__} is not JSON serializable")


def json_object(value: object, path: str = "$") -> JsonObject:
    parsed = json_value(value, path)
    if not isinstance(parsed, dict):
        raise TypeError(f"{path}: must serialize to a JSON object")
    return parsed


def dumps(value: object) -> str:
    return json.dumps(
        json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def loads(text: str | bytes) -> JsonValue:
    return json_value(json.loads(text))


def serialize[T](adapter: Schema[T], value: T) -> JsonValue:
    reject_nonfinite(value)
    return json_value(adapter.dump(value))


def validate[T](adapter: Schema[T], value: object, *, strict: bool = True) -> T:
    reject_nonfinite(value)
    result = adapter.validate(value, strict=strict)
    reject_nonfinite(result)
    return result


def describe[T](adapter: Schema[T], label: str) -> JsonObject:
    """Reject uninspectable schema adapters before opening their data source."""
    try:
        return json_object(adapter.json_schema())
    except Exception as exc:
        raise ConfigurationError(f"{label} schema is not inspectable: {exc}") from exc


def normalize_score(raw: object, name: str) -> JsonObject:
    """Normalize one scorer's numeric/null result under its declared metric name."""
    nonempty("score name", name)
    if isinstance(raw, Score):
        value = raw.value
        metadata = raw.metadata
    else:
        value = raw
        metadata = {}
    if value is not None and (
        type(value) not in (float, int) or not math.isfinite(cast(float, value))
    ):
        raise ValueError(f"Score {name!r} must be a finite number or None")
    normalized = cast(float | int | None, value)
    return {"name": name, "value": normalized, "metadata": json_object(metadata)}


def numeric_stats(values: Sequence[float]) -> JsonObject:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "mean": None, "min": None, "max": None, "p50": None, "p95": None}
    return {
        "count": len(ordered),
        "mean": math.fsum(x / len(ordered) for x in ordered),
        "min": ordered[0],
        "max": ordered[-1],
        "p50": ordered[max(0, math.ceil(0.50 * len(ordered)) - 1)],
        "p95": ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)],
    }
