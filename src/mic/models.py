"""Public authoring models, definition descriptors, and run results."""

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Generic, Literal, TypeVar

from .errors import ConfigurationError, MissingExpectedError
from .schema import Schema

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

_I = TypeVar("_I")
_O = TypeVar("_O")

# Python 3.12 has no stdlib TypeVar defaults. Keep defaults visible to type
# checkers; ScoreContext also fills its omitted runtime arguments below.
if TYPE_CHECKING:
    from typing_extensions import TypeVar as DefaultTypeVar

    _E = DefaultTypeVar("_E", default=_O)
    _M = DefaultTypeVar("_M", default=JsonObject)
else:
    _E = TypeVar("_E")
    _M = TypeVar("_M")


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
class CaseSchema[I, E, M]:
    input: Schema[I]
    expected: Schema[E]
    metadata: Schema[M]
    expected_policy: Literal["required", "optional"] = "required"


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


@dataclass(frozen=True)
class TaskContext(Generic[_M]):
    """Execution coordinates and optional metadata; never the reference answer."""

    case_id: str
    trial: int
    metadata: _M | None


@dataclass(frozen=True)
class ScoreContext(Generic[_I, _O, _E, _M]):
    input: _I
    output: _O
    expected: _E | Missing
    metadata: _M | None
    case_id: str = ""
    trial: int = 1

    if not TYPE_CHECKING:

        def __class_getitem__(cls, parameters: object) -> object:
            args = parameters if isinstance(parameters, tuple) else (parameters,)
            if len(args) == 2:
                args = (*args, args[1], JsonObject)
            elif len(args) == 3:
                args = (*args, JsonObject)
            return super().__class_getitem__(args)

    def require_expected(self) -> _E:
        if isinstance(self.expected, Missing):
            raise MissingExpectedError(f"Case {self.case_id!r} has no expected value")
        return self.expected


@dataclass(frozen=True)
class Score:
    value: float | None
    metadata: JsonObject = field(default_factory=dict[str, JsonValue])


@dataclass(frozen=True)
class TaskResult[O]:
    output: O
    metadata: JsonObject = field(default_factory=dict[str, JsonValue])


type Task[I, O, M] = Callable[[TaskContext[M], I], O | TaskResult[O] | Awaitable[O | TaskResult[O]]]
type ScoreValue = Score | float | int | None
type Scoring[I, O, E, M] = Callable[[ScoreContext[I, O, E, M]], ScoreValue | Awaitable[ScoreValue]]
type SourceFactory = Callable[[], object | Awaitable[object]]
type RowMapper = Callable[[object], RawCase]


@dataclass(frozen=True)
class Dataset[I, E, M]:
    name: str
    schema: CaseSchema[I, E, M]
    factory: SourceFactory = field(repr=False)
    map_row: RowMapper = field(repr=False)


@dataclass(frozen=True)
class Scorer[I, O, E, M]:
    name: str
    function: Scoring[I, O, E, M] = field(repr=False)
    requires_expected: bool = True


class EvaluationDefinition:
    """Non-generic base for heterogeneous selections of typed evaluations."""


@dataclass(frozen=True)
class Evaluation[I, O, E, M](EvaluationDefinition):
    name: str
    dataset: Dataset[I, E, M]
    output: Schema[O]
    scorers: tuple[Scorer[I, O, E, M], ...]
    function: Task[I, O, M] = field(repr=False)
    trials: int = 1
    concurrency: int = 10
