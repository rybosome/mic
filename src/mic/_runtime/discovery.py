"""Explicit-module discovery; descriptor inspection never invokes a dataset factory."""

import importlib
from dataclasses import dataclass
from typing import Any

import mic
from mic.errors import ConfigurationError

type Definition = (
    mic.Dataset[Any, Any, Any] | mic.Evaluation[Any, Any, Any, Any] | mic.Scorer[Any, Any, Any, Any]
)


@dataclass(frozen=True)
class DefinitionInfo:
    kind: str
    name: str
    selector: str
    definition: Definition


def _kind(value: object) -> str | None:
    if isinstance(value, mic.Dataset):
        return "dataset"
    if isinstance(value, mic.Evaluation):
        return "eval"
    if isinstance(value, mic.Scorer):
        return "scorer"
    return None


def list_definitions(module_name: str) -> list[DefinitionInfo]:
    """Import only the requested trusted Python module, then inspect its exports."""
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        raise ConfigurationError(f"Cannot import {module_name!r}: {exc}") from exc
    result: list[DefinitionInfo] = []
    for symbol, value in sorted(vars(module).items()):
        kind = _kind(value)
        if not symbol.startswith("_") and kind is not None:
            result.append(DefinitionInfo(kind, value.name, f"{module_name}:{symbol}", value))
    return result


def resolve_definition(selector: str) -> Definition:
    """Exact ``module:symbol`` selection; no substring matching or global registry."""
    if selector.count(":") != 1:
        raise ConfigurationError("Select an exact definition with module:symbol")
    module_name, symbol = selector.split(":")
    if not module_name or not symbol.isidentifier() or symbol.startswith("_"):
        raise ConfigurationError("Select an exact public definition with module:symbol")
    for definition in list_definitions(module_name):
        if definition.selector == selector:
            return definition.definition
    raise ConfigurationError(f"No mic definition found at {selector!r}")
