"""Declarative, side-effect-free authoring decorators."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Literal, Protocol, cast, overload

from .models import (
    CaseSchema,
    Dataset,
    EvalSpec,
    JsonObject,
    JsonValue,
    RowMapper,
    Scorer,
    Scoring,
    SourceFactory,
    Task,
)
from .schema import Schema
from .schema import schema as native_schema

if TYPE_CHECKING:
    from typing_extensions import TypeForm


def adapter[T](annotation: TypeForm[T] | Schema[T]) -> Schema[T]:
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
    strict: bool = True,
) -> CaseSchema[I, E, M]: ...


@overload
def case_schema[I, E](
    *,
    input: TypeForm[I] | Schema[I],
    expected: TypeForm[E] | Schema[E],
    expected_policy: Literal["required", "optional"] = "required",
    strict: bool = True,
) -> CaseSchema[I, E, JsonObject]: ...


def case_schema[I, E, M](
    *,
    input: TypeForm[I] | Schema[I],
    expected: TypeForm[E] | Schema[E],
    metadata: TypeForm[M] | Schema[M] | None = None,
    expected_policy: Literal["required", "optional"] = "required",
    strict: bool = True,
) -> CaseSchema[I, E, M] | CaseSchema[I, E, JsonObject]:
    if metadata is None:
        return CaseSchema(
            adapter(input),
            adapter(expected),
            native_schema(dict[str, JsonValue]),
            expected_policy,
            strict,
        )
    return CaseSchema(adapter(input), adapter(expected), adapter(metadata), expected_policy, strict)


def dataset[I, E, M](
    *,
    name: str,
    schema: CaseSchema[I, E, M],
    map_row: RowMapper | None = None,
) -> Callable[[SourceFactory], Dataset[I, E, M]]:
    from .datasets import envelope

    def decorate(factory: SourceFactory) -> Dataset[I, E, M]:
        return Dataset(name, schema, factory, map_row if map_row is not None else envelope)

    return decorate


class ScorerDecorator(Protocol):
    def __call__[I, O, E, M](self, fn: Scoring[I, O, E, M]) -> Scorer[I, O, E, M]: ...


def scorer(
    *,
    name: str,
    requires_expected: bool = True,
    metrics: tuple[str, ...] | None = None,
) -> ScorerDecorator:
    def decorate[I, O, E, M](fn: Scoring[I, O, E, M]) -> Scorer[I, O, E, M]:
        return Scorer(name, fn, metrics if metrics is not None else (name,), requires_expected)

    return decorate


def eval[I, O, E, M](
    *,
    name: str,
    dataset: Dataset[I, E, M],
    output: TypeForm[O] | Schema[O],
    scorers: Sequence[Scorer[I, O, E, M]],
    trials: int = 1,
    concurrency: int = 10,
    skip: bool = False,
    model_preset: str | None = None,
) -> Callable[[Task[I, O, E, M]], EvalSpec[I, O, E, M]]:
    def decorate(fn: Task[I, O, E, M]) -> EvalSpec[I, O, E, M]:
        return EvalSpec(
            name,
            dataset,
            adapter(output),
            tuple(scorers),
            fn,
            trials,
            concurrency,
            skip,
            model_preset,
        )

    return decorate
