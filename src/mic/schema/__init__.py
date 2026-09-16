"""Strict schemas for stdlib types, with no third-party runtime dependencies.

Dataclass mappings run constructors and ``__post_init__`` once; native instances
are revalidated and copied without rerunning initialization. Conversions belong
in a row mapper or an explicit schema adapter, rather than ``strict=False``.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, cast

from ._compiler import Compiler as _Compiler
from ._compiler import check_recursion as _check_recursion
from ._contracts import Document as _Document
from ._contracts import Node as _Node
from ._contracts import Schema, SchemaError

if TYPE_CHECKING:
    from typing_extensions import TypeForm
else:
    TypeForm = type

__all__ = ["NativeSchema", "Schema", "SchemaError", "schema"]


@dataclasses.dataclass(frozen=True)
class NativeSchema[T]:
    _root: _Node

    def validate(self, value: object, *, strict: bool = True) -> T:
        if strict is not True:
            raise SchemaError(
                "native schemas require strict=True; use a mapper or explicit adapter for conversion"
            )
        try:
            return cast(T, self._root.read(value, "$", set()))
        except RecursionError as exc:
            raise SchemaError("value nesting exceeds the Python recursion limit") from exc

    def dump(self, value: T) -> object:
        normalized = self.validate(value)
        try:
            return self._root.write(normalized, "$", set())
        except RecursionError as exc:
            raise SchemaError("value nesting exceeds the Python recursion limit") from exc

    def json_schema(self) -> dict[str, object]:
        document = _Document()
        result = self._root.describe(document)
        if document.definitions:
            result["$defs"] = document.definitions
        return result


def schema[T](annotation: TypeForm[T]) -> Schema[T]:
    """Compile a supported stdlib annotation; reject unsupported types immediately."""
    compiler = _Compiler()
    root = compiler.build(annotation)
    checked: set[_Node] = set()
    for reference in compiler.memo.values():
        _check_recursion(reference, set(), checked)
    _check_recursion(root, set(), checked)
    return NativeSchema[T](root)
