"""Opt-in Pydantic schemas without a Pydantic dependency in the mic core.

Importing this module is cheap and dependency-free. Pydantic is imported only
when ``pydantic_schema(...)`` is called, so definitions that use only dataclasses
and standard Python types can run without it installed.
"""

from __future__ import annotations

import dataclasses
import importlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

from mic._runtime.validation import dumps, json_value
from mic.errors import ConfigurationError

if TYPE_CHECKING:
    from pydantic import TypeAdapter

    from mic.schema import Schema

_NOT_SET = object()


def _reject_nonfinite(
    value: object,
    base_model: type[object],
    path: str = "$",
    seen: set[int] | None = None,
) -> None:
    """Inspect native model values before serializers can replace NaN with null."""
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
            _reject_nonfinite(key, base_model, f"{path}[key]", visited)
            _reject_nonfinite(child, base_model, f"{path}.{key}", visited)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for index, child in enumerate(cast(Sequence[object], value)):
            _reject_nonfinite(child, base_model, f"{path}[{index}]", visited)
    elif isinstance(value, base_model):
        # BaseModel is loaded with the optional SDK. Avoid coupling core walkers
        # to Pydantic's runtime classes or dropping allowed extra model fields.
        fields = cast(Mapping[str, object], getattr(type(value), "model_fields"))
        for key in fields:
            child: object = getattr(value, key, _NOT_SET)
            if child is not _NOT_SET:
                _reject_nonfinite(child, base_model, f"{path}.{key}", visited)
        extra: object = getattr(value, "model_extra", None)
        if extra is not None:
            _reject_nonfinite(extra, base_model, path, visited)
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        for item in dataclasses.fields(value):
            _reject_nonfinite(getattr(value, item.name), base_model, f"{path}.{item.name}", visited)


@dataclass(frozen=True)
class _PydanticSchema[T]:
    adapter: TypeAdapter[T]
    base_model: type[object]

    def validate(self, value: object, *, strict: bool = True) -> T:
        _reject_nonfinite(value, self.base_model)
        try:
            encoded = dumps(value)
        except (TypeError, ValueError):
            # Pydantic trusts existing instances by default. Serialize and hydrate
            # a fresh instance to enforce constraints on model_construct()/mutated
            # models, and to isolate generator-reused nested native values.
            projected = self.adapter.dump_python(
                cast(T, value), mode="json", warnings="error", by_alias=False
            )
            encoded = dumps(projected)
        result = self.adapter.validate_json(encoded, strict=strict, by_name=True)
        # User validators may themselves produce nonfinite values.
        _reject_nonfinite(result, self.base_model)
        return result

    def dump(self, value: T) -> object:
        _reject_nonfinite(value, self.base_model)
        return json_value(
            self.adapter.dump_python(value, mode="json", warnings="error", by_alias=False)
        )

    def json_schema(self) -> dict[str, object]:
        # Python field names are the canonical keys in persisted mic snapshots.
        return cast(dict[str, object], self.adapter.json_schema(by_alias=False))


def pydantic_schema[T](annotation: type[T] | TypeAdapter[T]) -> Schema[T]:
    """Adapt a Pydantic model or TypeAdapter to mic's ordinary Schema contract.

    Use ``TypeAdapter(...)`` for constrained/Annotated/union annotations when
    static inference cannot express them as ``type[T]``. JSON mappings hydrate
    native models and nested dataclasses; strict primitive validation is retained.
    Dumps use Python field names, while validation accepts field names or aliases.
    """
    try:
        sdk: Any = importlib.import_module("pydantic")
    except ImportError as exc:
        raise ConfigurationError(
            "Pydantic schemas require the optional extra: pip install mic-evals[pydantic]"
        ) from exc
    adapter = cast(
        "TypeAdapter[T]",
        annotation if isinstance(annotation, sdk.TypeAdapter) else sdk.TypeAdapter(annotation),
    )
    return _PydanticSchema(adapter, cast(type[object], sdk.BaseModel))
