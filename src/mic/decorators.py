"""Declarative, side-effect-free authoring decorators."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from functools import wraps
from typing import TYPE_CHECKING, Literal, Protocol, cast, overload

from .errors import ConfigurationError
from .models import (
    CaseSchema,
    Dataset,
    Evaluation,
    JsonObject,
    JsonValue,
    RowMapper,
    Scorer,
    Scoring,
    SourceFactory,
    Task,
    TaskContext,
    TaskResult,
)
from .schema import Schema
from .schema import schema as native_schema

if TYPE_CHECKING:
    from typing_extensions import TypeForm


def _schema_for[T](annotation: TypeForm[T] | Schema[T]) -> Schema[T]:
    if not isinstance(annotation, type) and all(
        callable(getattr(annotation, name, None)) for name in ("validate", "dump", "json_schema")
    ):
        return cast(Schema[T], annotation)
    # A caller supplying an existing Pydantic model/TypeAdapter has already opted
    # into that library. Ordinary dataclasses never import or select it implicitly.
    bases = annotation.__mro__ if isinstance(annotation, type) else type(annotation).__mro__
    if any(base.__module__.startswith("pydantic.") for base in bases):
        from .integrations.pydantic import pydantic_schema

        adapt = cast(Callable[[object], Schema[T]], pydantic_schema)
        return adapt(annotation)
    return native_schema(cast(type[T], annotation))


@overload
def case_schema[I, E, M](
    *,
    input: TypeForm[I] | Schema[I],
    expected: TypeForm[E] | Schema[E],
    metadata: TypeForm[M] | Schema[M],
    expected_policy: Literal["required", "optional"] = "required",
) -> CaseSchema[I, E, M]: ...


@overload
def case_schema[I, E](
    *,
    input: TypeForm[I] | Schema[I],
    expected: TypeForm[E] | Schema[E],
    expected_policy: Literal["required", "optional"] = "required",
) -> CaseSchema[I, E, JsonObject]: ...


def case_schema[I, E, M](
    *,
    input: TypeForm[I] | Schema[I],
    expected: TypeForm[E] | Schema[E],
    metadata: TypeForm[M] | Schema[M] | None = None,
    expected_policy: Literal["required", "optional"] = "required",
) -> CaseSchema[I, E, M] | CaseSchema[I, E, JsonObject]:
    if metadata is None:
        return CaseSchema(
            _schema_for(input),
            _schema_for(expected),
            native_schema(dict[str, JsonValue]),
            expected_policy,
        )
    return CaseSchema(
        _schema_for(input), _schema_for(expected), _schema_for(metadata), expected_policy
    )


def dataset[I, E, M](
    *,
    name: str,
    schema: CaseSchema[I, E, M],
    map_row: RowMapper | None = None,
) -> Callable[[SourceFactory], Dataset[I, E, M]]:
    from ._runtime.materialization import map_envelope

    def decorate(factory: SourceFactory) -> Dataset[I, E, M]:
        return Dataset(name, schema, factory, map_row if map_row is not None else map_envelope)

    return decorate


class _ScorerDecorator(Protocol):
    def __call__[I, O, E, M](self, fn: Scoring[I, O, E, M]) -> Scorer[I, O, E, M]: ...


def scorer(
    *,
    name: str,
    requires_expected: bool = True,
) -> _ScorerDecorator:
    def decorate[I, O, E, M](fn: Scoring[I, O, E, M]) -> Scorer[I, O, E, M]:
        return Scorer(name, fn, requires_expected)

    return decorate


type _InputTask[I, O] = Callable[[I], O | TaskResult[O] | Awaitable[O | TaskResult[O]]]


class _TaskDecorator[I, O, E, M](Protocol):
    @overload
    def __call__(self, fn: _InputTask[I, O]) -> Evaluation[I, O, E, M]: ...

    @overload
    def __call__(self, fn: Task[I, O, E, M]) -> Evaluation[I, O, E, M]: ...


def _contextual_task[I, O, E, M](fn: _InputTask[I, O] | Task[I, O, E, M]) -> Task[I, O, E, M]:
    try:
        parameters = tuple(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("Task must have an inspectable signature") from exc
    positional = [p for p in parameters if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    if len(positional) not in (1, 2) or any(
        p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
        or (p.kind == p.KEYWORD_ONLY and p.default is p.empty)
        for p in parameters
    ):
        raise ConfigurationError(
            "Task must accept (input) or (context, input); variadic parameters and "
            "required keyword-only parameters are not supported"
        )
    if len(positional) == 2:
        return cast(Task[I, O, E, M], fn)

    input_task = cast(_InputTask[I, O], fn)
    # Normalize once, retaining provenance and the callback pool's async dispatch.
    # Never probe by invoking a task or retry a TypeError raised by its body.
    if inspect.iscoroutinefunction(fn):

        @wraps(input_task)
        async def async_task(ctx: TaskContext[E, M], value: I) -> O | TaskResult[O]:
            return await cast(Awaitable[O | TaskResult[O]], input_task(value))

        return async_task

    @wraps(input_task)
    def sync_task(
        ctx: TaskContext[E, M], value: I
    ) -> O | TaskResult[O] | Awaitable[O | TaskResult[O]]:
        return input_task(value)

    return sync_task


def eval[I, O, E, M](
    *,
    name: str,
    dataset: Dataset[I, E, M],
    output: TypeForm[O] | Schema[O],
    scorers: Sequence[Scorer[I, O, E, M]],
    trials: int = 1,
    concurrency: int = 10,
) -> _TaskDecorator[I, O, E, M]:
    def decorate(fn: _InputTask[I, O] | Task[I, O, E, M]) -> Evaluation[I, O, E, M]:
        return Evaluation(
            name,
            dataset,
            _schema_for(output),
            tuple(scorers),
            _contextual_task(fn),
            trials,
            concurrency,
        )

    return decorate
