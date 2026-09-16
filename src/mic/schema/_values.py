"""Validation and JSON projection for scalar, collection, and union values."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from enum import Enum
from typing import cast

from ._contracts import Document, Node, SchemaError, field_path, guard


class PrimitiveNode(Node):
    def __init__(self, annotation: type[object]) -> None:
        self.annotation = annotation

    def read(self, value: object, path: str, active: set[int]) -> object:
        if self.annotation is float and type(value) in (int, float):
            try:
                converted = float(cast(int | float, value))
            except OverflowError as exc:
                raise SchemaError("number is outside the finite float range", path=path) from exc
            if not math.isfinite(converted):
                raise SchemaError("nonfinite numbers are not supported", path=path)
            return converted
        if type(value) is not self.annotation:
            raise SchemaError(
                f"expected {self.annotation.__name__}, got {type(value).__name__}",
                path=path,
                priority=0,
            )
        return value

    def write(self, value: object, path: str, active: set[int]) -> object:
        return self.read(value, path, active)

    def describe(self, document: "Document") -> dict[str, object]:
        names: dict[type[object], str] = {
            str: "string",
            int: "integer",
            float: "number",
            bool: "boolean",
            type(None): "null",
        }
        return {"type": names[self.annotation]}


class JsonNode(Node):
    def read(self, value: object, path: str, active: set[int]) -> object:
        if value is None or type(value) in (str, int, bool):
            return value
        if type(value) is float:
            if not math.isfinite(value):
                raise SchemaError("nonfinite numbers are not supported", path=path)
            return value
        if isinstance(value, list):
            with guard(cast(object, value), path, active):
                return [
                    self.read(item, f"{path}[{index}]", active)
                    for index, item in enumerate(cast(list[object], value))
                ]
        if isinstance(value, Mapping):
            with guard(cast(object, value), path, active):
                result: dict[str, object] = {}
                for key, item in cast(Mapping[object, object], value).items():
                    if type(key) is not str:
                        raise SchemaError("JSON object keys must be strings", path=f"{path}[key]")
                    result[key] = self.read(item, field_path(path, key), active)
                return result
        raise SchemaError(
            f"expected finite JSON data, got {type(value).__name__}", path=path, priority=0
        )

    def write(self, value: object, path: str, active: set[int]) -> object:
        return self.read(value, path, active)

    def describe(self, document: "Document") -> dict[str, object]:
        return {}


class ListNode(Node):
    def __init__(self, item: Node, *, sequence: bool = False) -> None:
        self.item = item
        self.sequence = sequence

    def read(self, value: object, path: str, active: set[int]) -> object:
        if not isinstance(value, list) and not (
            self.sequence
            and isinstance(value, Sequence)
            and not isinstance(value, (str, bytes, bytearray))
        ):
            raise SchemaError(f"expected list, got {type(value).__name__}", path=path, priority=0)
        with guard(cast(object, value), path, active):
            return [
                self.item.read(item, f"{path}[{index}]", active)
                for index, item in enumerate(cast(Sequence[object], value))
            ]

    def write(self, value: object, path: str, active: set[int]) -> object:
        with guard(value, path, active):
            return [
                self.item.write(item, f"{path}[{index}]", active)
                for index, item in enumerate(cast(Sequence[object], value))
            ]

    def describe(self, document: "Document") -> dict[str, object]:
        return {"type": "array", "items": self.item.describe(document)}


class TupleNode(Node):
    def __init__(self, items: tuple[Node, ...], *, repeated: bool = False) -> None:
        self.items, self.repeated = items, repeated

    def _items(self, value: object, path: str) -> list[tuple[Node, object]]:
        if not isinstance(value, (list, tuple)):
            raise SchemaError(
                f"expected tuple or JSON array, got {type(value).__name__}", path=path, priority=0
            )
        values = cast(Sequence[object], value)
        if self.repeated:
            return [(self.items[0], item) for item in values]
        if len(values) != len(self.items):
            raise SchemaError(
                f"expected tuple of length {len(self.items)}, got {len(values)}", path=path
            )
        return list(zip(self.items, values, strict=True))

    def read(self, value: object, path: str, active: set[int]) -> object:
        pairs = self._items(value, path)
        with guard(value, path, active):
            return tuple(
                node.read(item, f"{path}[{index}]", active)
                for index, (node, item) in enumerate(pairs)
            )

    def write(self, value: object, path: str, active: set[int]) -> object:
        with guard(value, path, active):
            return [
                node.write(item, f"{path}[{index}]", active)
                for index, (node, item) in enumerate(self._items(value, path))
            ]

    def describe(self, document: "Document") -> dict[str, object]:
        if self.repeated:
            return {"type": "array", "items": self.items[0].describe(document)}
        return {
            "type": "array",
            "prefixItems": [node.describe(document) for node in self.items],
            "items": False,
            "minItems": len(self.items),
            "maxItems": len(self.items),
        }


class MapNode(Node):
    def __init__(self, item: Node) -> None:
        self.item = item

    def read(self, value: object, path: str, active: set[int]) -> object:
        if not isinstance(value, Mapping):
            raise SchemaError(f"expected object, got {type(value).__name__}", path=path, priority=0)
        with guard(cast(object, value), path, active):
            result: dict[str, object] = {}
            for key, item in cast(Mapping[object, object], value).items():
                if type(key) is not str:
                    raise SchemaError("object keys must be strings", path=f"{path}[key]")
                result[key] = self.item.read(item, field_path(path, key), active)
            return result

    def write(self, value: object, path: str, active: set[int]) -> object:
        with guard(value, path, active):
            return {
                key: self.item.write(item, field_path(path, key), active)
                for key, item in cast(dict[str, object], value).items()
            }

    def describe(self, document: "Document") -> dict[str, object]:
        return {"type": "object", "additionalProperties": self.item.describe(document)}


class UnionNode(Node):
    def __init__(self, choices: tuple[Node, ...]) -> None:
        self.choices = choices

    def _select(self, value: object, path: str, active: set[int]) -> tuple[Node, object]:
        errors: list[SchemaError] = []
        for choice in self.choices:
            try:
                return choice, choice.read(value, path, active)
            except SchemaError as exc:
                errors.append(exc)
        closest = max(errors, key=lambda error: (len(error.path), error.priority))
        raise SchemaError(
            f"no union variant matched; {closest.detail}",
            path=closest.path,
            priority=closest.priority,
        )

    def read(self, value: object, path: str, active: set[int]) -> object:
        return self._select(value, path, active)[1]

    def write(self, value: object, path: str, active: set[int]) -> object:
        choice, normalized = self._select(value, path, active)
        return choice.write(normalized, path, active)

    def describe(self, document: "Document") -> dict[str, object]:
        return {"anyOf": [node.describe(document) for node in self.choices]}


def _literal_json(value: object) -> object:
    raw = value.value if isinstance(value, Enum) else value
    if raw is not None and type(raw) not in (str, int, bool, float):
        raise SchemaError("Literal and Enum values must be JSON primitives")
    if type(raw) is float and not math.isfinite(raw):
        raise SchemaError("Literal and Enum values must be finite")
    return raw


class LiteralNode(Node):
    def __init__(self, values: tuple[object, ...]) -> None:
        self.values = values
        self.json_values = tuple(_literal_json(value) for value in values)

    def read(self, value: object, path: str, active: set[int]) -> object:
        for candidate, raw in zip(self.values, self.json_values, strict=True):
            if type(value) is type(candidate) and value == candidate:
                return candidate
            if isinstance(candidate, Enum) and type(value) is type(raw) and value == raw:
                return candidate
        raise SchemaError(f"expected one of {self.json_values!r}", path=path)

    def write(self, value: object, path: str, active: set[int]) -> object:
        return _literal_json(self.read(value, path, active))

    def describe(self, document: "Document") -> dict[str, object]:
        return {"enum": list(self.json_values)}
