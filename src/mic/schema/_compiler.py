"""Compile supported annotations and check recursive schema graphs."""

from __future__ import annotations

import dataclasses
import types
import typing
from collections.abc import Callable, Mapping, Sequence
from enum import Enum
from typing import cast

from ._contracts import Document, Node, SchemaError
from ._dataclasses import ABSENT, FieldSpec, ObjectNode
from ._values import JsonNode, ListNode, LiteralNode, MapNode, PrimitiveNode, TupleNode, UnionNode


class ReferenceNode(Node):
    def __init__(self, name: str) -> None:
        self.name = name
        self.target: Node | None = None

    def _target(self) -> Node:
        assert self.target is not None
        return self.target

    def read(self, value: object, path: str, active: set[int]) -> object:
        return self._target().read(value, path, active)

    def write(self, value: object, path: str, active: set[int]) -> object:
        return self._target().write(value, path, active)

    def describe(self, document: "Document") -> dict[str, object]:
        if isinstance(self._target(), ObjectNode):
            return self._target().describe(document)
        return document.reference(self, self.name, lambda: self._target().describe(document))


def _hints(cls: type[object]) -> dict[str, object]:
    try:
        return cast(
            dict[str, object],
            typing.get_type_hints(
                cls,
                localns={base.__name__: base for base in cls.__mro__},
                include_extras=True,
            ),
        )
    except (NameError, TypeError) as exc:
        raise SchemaError(
            f"cannot resolve annotations for {cls.__qualname__}: {exc}; define forward-referenced types at module scope"
        ) from exc


class Compiler:
    def __init__(self) -> None:
        self.memo: dict[object, ReferenceNode] = {}

    def build(self, annotation: object) -> Node:
        if annotation is None:
            annotation = type(None)
        if annotation is typing.Any or annotation is object:
            return JsonNode()
        if annotation in (str, int, float, bool, type(None)):
            return PrimitiveNode(cast(type[object], annotation))
        if isinstance(annotation, typing.TypeAliasType):
            if annotation in self.memo:
                return self.memo[annotation]
            reference = ReferenceNode(annotation.__name__)
            self.memo[annotation] = reference
            if annotation.__type_params__:
                raise SchemaError(
                    f"parameterized type alias {annotation.__name__} requires an explicit adapter"
                )
            try:
                resolved: object = annotation.__value__
            except (NameError, TypeError) as exc:
                raise SchemaError(
                    f"cannot resolve type alias {annotation.__name__}: {exc}"
                ) from exc
            reference.target = self.build(resolved)
            return reference
        origin: object = typing.get_origin(annotation)
        args = cast(tuple[object, ...], typing.get_args(annotation))
        if origin is typing.Union or origin is types.UnionType:
            return UnionNode(tuple(self.build(arg) for arg in args))
        if origin is typing.Literal:
            return LiteralNode(args)
        if origin is typing.Annotated:
            raise SchemaError("Annotated constraints require an explicit schema adapter")
        if annotation is list or annotation is Sequence or origin is list or origin is Sequence:
            return ListNode(
                self.build(args[0]) if args else JsonNode(),
                sequence=annotation is Sequence or origin is Sequence,
            )
        if annotation is tuple or origin is tuple:
            if annotation is tuple:
                return TupleNode((JsonNode(),), repeated=True)
            if len(args) == 2 and args[1] is Ellipsis:
                return TupleNode((self.build(args[0]),), repeated=True)
            return TupleNode(tuple(self.build(arg) for arg in args))
        if annotation is dict or annotation is Mapping or (origin is dict or origin is Mapping):
            if args and args[0] is not str:
                raise SchemaError("dictionary and Mapping schemas require string keys")
            return MapNode(self.build(args[1]) if args else JsonNode())
        if isinstance(annotation, type) and issubclass(annotation, Enum):
            return LiteralNode(tuple(annotation))
        if isinstance(annotation, type):
            annotation_type = cast(type[object], annotation)
            if dataclasses.is_dataclass(annotation_type) or typing.is_typeddict(annotation_type):
                return self._object(annotation_type)
        raise SchemaError(f"unsupported native annotation {annotation!r}; provide a Schema adapter")

    def _object(self, cls: type[object]) -> Node:
        if cls in self.memo:
            return self.memo[cls]
        reference = ReferenceNode(cls.__qualname__)
        self.memo[cls] = reference
        hints = _hints(cls)
        fields: list[FieldSpec] = []
        if typing.is_typeddict(cls):
            required = cast(frozenset[str], getattr(cls, "__required_keys__"))
            for name, annotation in hints.items():
                origin: object = typing.get_origin(annotation)
                mandatory = name in required
                if origin is typing.Required or origin is typing.NotRequired:
                    mandatory = origin is typing.Required
                    annotation = typing.get_args(annotation)[0]
                fields.append(FieldSpec(name, self.build(annotation), required=mandatory))
            reference.target = ObjectNode(cls.__qualname__, tuple(fields), None)
            return reference
        for name, annotation in hints.items():
            if isinstance(annotation, dataclasses.InitVar):
                raise SchemaError(f"{cls.__qualname__}.{name}: InitVar fields are unsupported")
        if not dataclasses.is_dataclass(cls):
            raise SchemaError(f"expected dataclass, got {cls.__qualname__}")
        for field in cast(tuple[dataclasses.Field[object], ...], dataclasses.fields(cls)):
            if not field.init:
                raise SchemaError(
                    f"{cls.__qualname__}.{field.name}: init=False fields are unsupported"
                )
            annotation = hints.get(field.name, field.type)
            default: object = field.default
            has_default = default is not dataclasses.MISSING
            factory: Callable[[], object] | None = None
            if field.default_factory is not dataclasses.MISSING:
                factory = cast(Callable[[], object], field.default_factory)
            fields.append(
                FieldSpec(
                    field.name,
                    self.build(annotation),
                    default if has_default else ABSENT,
                    factory,
                    not has_default and factory is None,
                )
            )
        reference.target = ObjectNode(cls.__qualname__, tuple(fields), cls)
        return reference


def check_recursion(node: Node, active: set[Node], checked: set[Node]) -> None:
    """Reject recursion that never descends into a field, array item, or map value."""
    if node in active:
        raise SchemaError(
            "recursive annotation must descend through a dataclass, list, tuple, or map"
        )
    if node in checked:
        return
    active.add(node)
    if isinstance(node, ReferenceNode):
        assert node.target is not None
        check_recursion(node.target, active, checked)
    elif isinstance(node, UnionNode):
        for child in node.choices:
            check_recursion(child, active, checked)
    active.remove(node)
    checked.add(node)
