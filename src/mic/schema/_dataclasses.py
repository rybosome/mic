"""Object-field hydration and isolated dataclass reconstruction."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from typing import cast

from ._contracts import Document, Node, SchemaError, field_path, guard

ABSENT = object()


@dataclasses.dataclass(frozen=True)
class FieldSpec:
    name: str
    node: Node
    default: object = ABSENT
    factory: Callable[[], object] | None = None
    required: bool = True


class ObjectNode(Node):
    def __init__(self, name: str, fields: tuple[FieldSpec, ...], cls: type[object] | None) -> None:
        self.name, self.fields, self.cls = name, fields, cls
        self.names = {field.name for field in fields}

    def _state(self, value: object, path: str) -> None:
        state = getattr(value, "__dict__", None)
        if isinstance(state, dict):
            for name in cast(dict[object, object], state):
                if type(name) is not str:
                    raise SchemaError("instance state keys must be strings", path=f"{path}[key]")
                if name not in self.names:
                    raise SchemaError(
                        "undeclared dataclass instance state is unsupported",
                        path=field_path(path, name),
                    )
        for base in type(value).__mro__:
            slots: object = vars(base).get("__slots__", ())
            slot_names = (slots,) if isinstance(slots, str) else cast(tuple[str, ...], slots)
            for name in slot_names:
                if name.startswith("__") and not name.endswith("__"):
                    name = f"_{base.__name__.lstrip('_')}{name}"
                if (
                    name not in self.names
                    and name not in ("__dict__", "__weakref__")
                    and hasattr(value, name)
                ):
                    raise SchemaError(
                        "undeclared dataclass slot state is unsupported",
                        path=field_path(path, name),
                    )

    def _instance(self, value: object, path: str, active: set[int]) -> object:
        self._state(value, path)
        with guard(value, path, active):
            values: dict[str, object] = {}
            for field in self.fields:
                try:
                    raw = getattr(value, field.name)
                except AttributeError as exc:
                    raise SchemaError(
                        "dataclass instance is missing a field", path=field_path(path, field.name)
                    ) from exc
                values[field.name] = field.node.read(raw, field_path(path, field.name), active)
            assert self.cls is not None
            try:
                cloned = object.__new__(self.cls)
                for name, item in values.items():
                    object.__setattr__(cloned, name, item)
            except (AttributeError, TypeError) as exc:
                raise SchemaError(f"cannot rebuild dataclass instance: {exc}", path=path) from exc
            return cloned

    def read(self, value: object, path: str, active: set[int]) -> object:
        if self.cls is not None and type(value) is self.cls:
            return self._instance(value, path, active)
        if not isinstance(value, Mapping):
            raise SchemaError(
                f"expected {self.name} object, got {type(value).__name__}", path=path, priority=0
            )
        with guard(cast(object, value), path, active):
            raw_values = cast(Mapping[object, object], value)
            for key in raw_values:
                if type(key) is not str:
                    raise SchemaError("object keys must be strings", path=f"{path}[key]")
                if key not in self.names:
                    raise SchemaError("unknown field", path=field_path(path, key))
            values: dict[str, object] = {}
            for field in self.fields:
                child_path = field_path(path, field.name)
                if field.name in raw_values:
                    raw = raw_values[field.name]
                elif field.default is not ABSENT:
                    raw = field.default
                elif field.factory is not None:
                    try:
                        raw = field.factory()
                    except Exception as exc:
                        raise SchemaError(
                            f"default factory failed: {exc}", path=child_path
                        ) from exc
                elif field.required:
                    raise SchemaError("missing required field", path=child_path)
                else:
                    continue
                values[field.name] = field.node.read(raw, child_path, active)
            if self.cls is None:
                return values
            try:
                constructed = cast(Callable[..., object], self.cls)(**values)
            except Exception as exc:
                raise SchemaError(f"dataclass constructor failed: {exc}", path=path) from exc
            if type(constructed) is not self.cls:
                raise SchemaError("dataclass constructor returned an unexpected type", path=path)
            # Validate changes made by __post_init__, then copy without running it again.
            return self._instance(constructed, path, active)

    def write(self, value: object, path: str, active: set[int]) -> object:
        with guard(value, path, active):
            if self.cls is None:
                items = cast(dict[str, object], value)
                return {
                    field.name: field.node.write(
                        items[field.name], field_path(path, field.name), active
                    )
                    for field in self.fields
                    if field.name in items
                }
            return {
                field.name: field.node.write(
                    getattr(value, field.name), field_path(path, field.name), active
                )
                for field in self.fields
            }

    def describe(self, document: "Document") -> dict[str, object]:
        def body() -> dict[str, object]:
            return {
                "title": self.name,
                "type": "object",
                "properties": {field.name: field.node.describe(document) for field in self.fields},
                "required": [field.name for field in self.fields if field.required],
                "additionalProperties": False,
            }

        return document.reference(self, self.name, body)
