"""Public value models and typed callback descriptors."""

import math
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from .errors import ConfigurationError, MissingExpectedError
from .schema import Schema

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


@dataclass(frozen=True)
class Missing:
    """An absent field, distinct from a present JSON null."""


MISSING = Missing()


@dataclass(frozen=True)
class RawCase:
    input: object
    expected: object = MISSING
    metadata: object | None = None
    id: str | None = None
    provenance: JsonObject = field(default_factory=dict[str, JsonValue])


@dataclass(frozen=True)
class Case[I, E, M]:
    id: str
    input: I
    expected: E | Missing
    metadata: M | None
    provenance: JsonObject = field(default_factory=dict[str, JsonValue])


@dataclass(frozen=True)
class CaseSchema[I, E, M]:
    input: Schema[I]
    expected: Schema[E]
    metadata: Schema[M]
    expected_policy: Literal["required", "optional"] = "required"
    strict: bool = True


@dataclass(frozen=True)
class ReadLimits:
    max_rows: int = 10_000
    max_bytes: int = 64 * 1024 * 1024
    max_record_bytes: int = 1024 * 1024
    timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        for key, value in (
            ("max_rows", self.max_rows),
            ("max_bytes", self.max_bytes),
            ("max_record_bytes", self.max_record_bytes),
        ):
            if type(value) is not int or value <= 0:
                raise ConfigurationError(f"{key} must be a positive integer")
        if (
            isinstance(self.timeout_seconds, bool)
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ConfigurationError("timeout_seconds must be a finite positive number")


class DatasetHandle(Protocol):
    @property
    def provider(self) -> str: ...


class DatasetRead(Protocol):
    @property
    def provenance(self) -> JsonObject: ...

    def rows(self) -> AsyncIterator[object]: ...


class DatasetLoader[H](Protocol):
    def open(
        self, handle: H, *, limits: ReadLimits
    ) -> AbstractAsyncContextManager[DatasetRead]: ...


@dataclass(frozen=True)
class TaskContext[E, M]:
    case_id: str
    trial: int
    expected: E | Missing
    metadata: M | None
    model_preset: str | None = None

    def require_expected(self) -> E:
        if isinstance(self.expected, Missing):
            raise MissingExpectedError(f"Case {self.case_id!r} has no expected value")
        return self.expected


@dataclass(frozen=True)
class ScoreContext[I, O, E, M]:
    input: I
    output: O
    expected: E | Missing
    metadata: M | None
    case_id: str = ""
    trial: int = 1

    def require_expected(self) -> E:
        if isinstance(self.expected, Missing):
            raise MissingExpectedError(f"Case {self.case_id!r} has no expected value")
        return self.expected


@dataclass(frozen=True)
class Score:
    name: str
    value: float | None
    metadata: JsonObject = field(default_factory=dict[str, JsonValue])


@dataclass(frozen=True)
class TaskResult[O]:
    output: O
    metadata: JsonObject = field(default_factory=dict[str, JsonValue])


type Task[I, O, E, M] = Callable[
    [TaskContext[E, M], I], O | TaskResult[O] | Awaitable[O | TaskResult[O]]
]
type Scoring[I, O, E, M] = Callable[
    [ScoreContext[I, O, E, M]], Score | Sequence[Score] | Awaitable[Score | Sequence[Score]]
]
type SourceFactory = Callable[[], object | Awaitable[object]]
type RowMapper = Callable[[object], RawCase]


@dataclass(frozen=True)
class Dataset[I, E, M]:
    name: str
    schema: CaseSchema[I, E, M]
    __wrapped__: SourceFactory
    map_row: RowMapper


@dataclass(frozen=True)
class Scorer[I, O, E, M]:
    name: str
    __wrapped__: Scoring[I, O, E, M]
    metrics: tuple[str, ...]
    requires_expected: bool = True


@dataclass(frozen=True)
class EvalSpec[I, O, E, M]:
    name: str
    dataset: Dataset[I, E, M]
    output: Schema[O]
    scorers: tuple[Scorer[I, O, E, M], ...]
    __wrapped__: Task[I, O, E, M]
    trials: int = 1
    concurrency: int = 10
    skip: bool = False
    model_preset: str | None = None


class Reporter(Protocol):
    @property
    def name(self) -> str: ...

    async def prepare(self) -> None: ...

    async def report(self, manifest: JsonObject, cases: Sequence[JsonObject]) -> JsonObject: ...


@dataclass(frozen=True)
class RunResult:
    manifest: JsonObject
    cases: list[JsonObject]
    output_dir: Path
    exit_code: int

    @property
    def run_id(self) -> str:
        return str(self.manifest["run_id"])

    @property
    def status(self) -> str:
        return str(self.manifest["status"])
