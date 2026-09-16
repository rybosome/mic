"""Schema contracts, errors, value paths, and JSON Schema document references."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Protocol, runtime_checkable


class SchemaError(ValueError):
    """An unsupported schema or invalid value, with its location in the value."""

    def __init__(self, message: str, *, path: str = "$", priority: int = 1) -> None:
        self.path = path
        self.detail = message
        self.priority = priority
        super().__init__(f"{path}: {message}")


@runtime_checkable
class Schema[T](Protocol):
    def validate(self, value: object, *, strict: bool = True) -> T: ...

    def dump(self, value: T) -> object: ...

    def json_schema(self) -> dict[str, object]: ...


def field_path(path: str, key: str) -> str:
    return f"{path}.{key}" if key.isidentifier() else f"{path}[{json.dumps(key)}]"


@contextmanager
def guard(value: object, path: str, active: set[int]) -> Generator[None]:
    identity = id(value)
    if identity in active:
        raise SchemaError("cyclic values are not supported", path=path)
    active.add(identity)
    try:
        yield
    finally:
        active.remove(identity)


class Node:
    def read(self, value: object, path: str, active: set[int]) -> object:
        raise NotImplementedError

    def write(self, value: object, path: str, active: set[int]) -> object:
        raise NotImplementedError

    def describe(self, document: "Document") -> dict[str, object]:
        raise NotImplementedError


class Document:
    def __init__(self) -> None:
        self.definitions: dict[str, object] = {}
        self.names: dict[Node, str] = {}

    def reference(
        self, node: Node, proposed: str, body: Callable[[], dict[str, object]]
    ) -> dict[str, object]:
        if node not in self.names:
            base = re.sub(r"[^A-Za-z0-9_-]", "_", proposed) or "Value"
            name = base
            index = 2
            while name in self.definitions:
                name = f"{base}_{index}"
                index += 1
            self.names[node] = name
            self.definitions[name] = {}
            self.definitions[name] = body()
        return {"$ref": f"#/$defs/{self.names[node]}"}
