"""Compact final results and serializable, bounded execution outcomes."""

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal, cast

from .models import JsonObject, JsonValue
from .summaries import EvaluationSummary, RequirementResult

type RunStatus = Literal["completed", "failed", "cancelled"]


@dataclass(frozen=True)
class Failure:
    phase: str
    type: str
    message: str


def failure(phase: str, exc: BaseException) -> Failure:
    """Never serialize arbitrary exception text, response bodies, or locals."""
    return Failure(phase, type(exc).__name__, f"{phase} failed ({type(exc).__name__})")


@dataclass(frozen=True)
class SourceSummary:
    name: str
    records_seen: int
    records_accepted: int
    records_rejected: int
    exhausted: bool
    digest: str | None
    provenance: JsonObject = field(default_factory=dict[str, JsonValue])
    error: Failure | None = None


@dataclass(frozen=True)
class RunInfo:
    run_id: str
    started_at: str
    tasks: JsonObject


@dataclass(frozen=True)
class EvaluationOutcome:
    run_id: str
    status: RunStatus
    exit_code: int
    summary: EvaluationSummary
    sources: Mapping[str, SourceSummary]
    requirements: tuple[RequirementResult, ...]
    failures: tuple[Failure, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "sources", MappingProxyType(dict(self.sources)))


@dataclass(frozen=True)
class SinkReceipt:
    name: str
    status: RunStatus
    events: int = 0
    details: JsonObject = field(default_factory=dict[str, JsonValue])
    error: Failure | None = None


@dataclass(frozen=True)
class RunResult(EvaluationOutcome):
    sinks: tuple[SinkReceipt, ...] = ()
    output_dir: Path | None = None
    info: RunInfo | None = None

    def to_json(self) -> JsonObject:
        result = cast(JsonObject, to_json(self))
        return {"schema_version": "mic-run-v4", **result}


def to_json(value: object) -> JsonValue:
    """Project immutable models without deepcopying mappingproxy objects."""
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_json(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {str(k): to_json(v) for k, v in cast(Mapping[object, object], value).items()}
    if isinstance(value, (tuple, list)):
        return [to_json(v) for v in cast(tuple[object, ...] | list[object], value)]
    if isinstance(value, Path):
        return str(value)
    from ._runtime.validation import json_value

    return json_value(value)
