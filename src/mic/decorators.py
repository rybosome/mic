"""Declarative, side-effect-free authoring decorators."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from functools import wraps
from types import UnionType
from typing import (
    TYPE_CHECKING,
    Any,
    Literal,
    Never,
    Protocol,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
    overload,
)

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


class _Unset:
    pass


_UNSET = _Unset()


def _definition_name(fn: object, name: str | None) -> str:
    if name is not None:
        return name
    inferred: object = getattr(fn, "__name__", None)
    if not isinstance(inferred, str) or not inferred.strip():
        raise ConfigurationError("Cannot infer definition name; provide name= explicitly")
    return inferred


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


@overload
def dataset[I, E, M](
    *,
    name: str | None = None,
    schema: CaseSchema[I, E, M],
    map_row: RowMapper | None = None,
) -> Callable[[SourceFactory], Dataset[I, E, M]]: ...


@overload
def dataset[I, E, M](
    *,
    name: str | None = None,
    input: TypeForm[I] | Schema[I],
    expected: TypeForm[E] | Schema[E],
    metadata: TypeForm[M] | Schema[M],
    expected_policy: Literal["required", "optional"] = "required",
    map_row: RowMapper | None = None,
) -> Callable[[SourceFactory], Dataset[I, E, M]]: ...


@overload
def dataset[I, E](
    *,
    name: str | None = None,
    input: TypeForm[I] | Schema[I],
    expected: TypeForm[E] | Schema[E],
    expected_policy: Literal["required", "optional"] = "required",
    map_row: RowMapper | None = None,
) -> Callable[[SourceFactory], Dataset[I, E, JsonObject]]: ...


def dataset[I, E, M](
    *,
    name: str | None = None,
    schema: CaseSchema[I, E, M] | _Unset = _UNSET,
    input: TypeForm[I] | Schema[I] | _Unset = _UNSET,
    expected: TypeForm[E] | Schema[E] | _Unset = _UNSET,
    metadata: TypeForm[M] | Schema[M] | _Unset = _UNSET,
    expected_policy: Literal["required", "optional"] | _Unset = _UNSET,
    map_row: RowMapper | None = None,
) -> Callable[[SourceFactory], Dataset[I, E, M]]:
    from ._runtime.datasets import map_envelope

    if not isinstance(schema, _Unset):
        if any(
            not isinstance(value, _Unset) for value in (input, expected, metadata, expected_policy)
        ):
            raise ConfigurationError(
                "Use schema= or input=/expected=/metadata=/expected_policy=, not both"
            )
        resolved = schema
    else:
        if isinstance(input, _Unset) or isinstance(expected, _Unset):
            raise ConfigurationError("Dataset requires schema= or both input= and expected=")
        policy = "required" if isinstance(expected_policy, _Unset) else expected_policy
        if isinstance(metadata, _Unset):
            resolved = cast(
                CaseSchema[I, E, M],
                case_schema(
                    input=input,
                    expected=expected,
                    expected_policy=policy,
                ),
            )
        else:
            resolved = case_schema(
                input=input,
                expected=expected,
                metadata=metadata,
                expected_policy=policy,
            )

    def decorate(factory: SourceFactory) -> Dataset[I, E, M]:
        return Dataset(
            _definition_name(factory, name),
            resolved,
            factory,
            map_row if map_row is not None else map_envelope,
        )

    return decorate


class _ScorerDecorator(Protocol):
    def __call__[I, O, E, M](self, fn: Scoring[I, O, E, M]) -> Scorer[I, O, E, M]: ...


def scorer(
    *,
    name: str | None = None,
    requires_expected: bool = True,
) -> _ScorerDecorator:
    def decorate[I, O, E, M](fn: Scoring[I, O, E, M]) -> Scorer[I, O, E, M]:
        return Scorer(_definition_name(fn, name), fn, requires_expected)

    return decorate


type _InputTask[I, O] = Callable[[I], O | TaskResult[O] | Awaitable[O | TaskResult[O]]]


class _TaskDecorator[I, O, E, M](Protocol):
    @overload
    def __call__(self, fn: _InputTask[I, O]) -> Evaluation[I, O, E, M]: ...

    @overload
    def __call__(self, fn: Task[I, O, M]) -> Evaluation[I, O, E, M]: ...


class _InferredTaskDecorator[I, E, M](Protocol):
    @overload
    def __call__[O](self, fn: _InputTask[I, O]) -> Evaluation[I, O, E, M]: ...

    @overload
    def __call__[O](self, fn: Task[I, O, M]) -> Evaluation[I, O, E, M]: ...


def _result_annotation(annotation: object) -> object:
    if annotation is Any or annotation is object:
        raise ConfigurationError("Return annotation must identify an output schema")
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (TaskResult, Awaitable, Coroutine):
        if not args:
            raise ConfigurationError("Return wrapper needs a concrete output type")
        return _result_annotation(args[-1])
    if origin in (Union, UnionType):
        return Union[tuple(_result_annotation(arg) for arg in args)]
    return annotation


def _inferred_output(fn: object) -> object:
    # Resolve only the return annotation, not unrelated input/context annotations.
    # Never execute the task to learn its output contract.
    try:
        target = inspect.unwrap(fn) if callable(fn) else fn
        annotation = inspect.signature(cast(Callable[..., object], target)).return_annotation
        if annotation is inspect.Signature.empty:
            raise ValueError("missing return annotation")

        def hints() -> None:
            pass

        hints.__annotations__ = {"return": annotation}
        namespace = getattr(target, "__globals__", {})
        resolved = get_type_hints(hints, globalns=namespace, include_extras=True)["return"]
        return _result_annotation(resolved)
    except Exception as exc:
        raise ConfigurationError(
            "Cannot infer output schema; use a resolvable return annotation or explicit output="
        ) from exc


def _contextual_task[I, O, M](fn: _InputTask[I, O] | Task[I, O, M]) -> Task[I, O, M]:
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
        return cast(Task[I, O, M], fn)

    input_task = cast(_InputTask[I, O], fn)
    # Normalize once, retaining provenance and the callback pool's async dispatch.
    # Never probe by invoking a task or retry a TypeError raised by its body.
    if inspect.iscoroutinefunction(fn):

        @wraps(input_task)
        async def async_task(ctx: TaskContext[M], value: I) -> O | TaskResult[O]:
            return await cast(Awaitable[O | TaskResult[O]], input_task(value))

        return async_task

    @wraps(input_task)
    def sync_task(
        ctx: TaskContext[M], value: I
    ) -> O | TaskResult[O] | Awaitable[O | TaskResult[O]]:
        return input_task(value)

    return sync_task


@overload
def eval[I, O, E, M](
    *,
    name: str | None = None,
    dataset: Dataset[I, E, M],
    output: TypeForm[O] | Schema[O],
    scorers: Sequence[Scorer[I, O, E, M]],
    trials: int = 1,
    concurrency: int = 10,
) -> _TaskDecorator[I, O, E, M]: ...


@overload
def eval[I, E, M](
    *,
    name: str | None = None,
    dataset: Dataset[I, E, M],
    scorers: Sequence[Never],
    trials: int = 1,
    concurrency: int = 10,
) -> _InferredTaskDecorator[I, E, M]: ...


@overload
def eval[I, O, E, M](
    *,
    name: str | None = None,
    dataset: Dataset[I, E, M],
    scorers: Sequence[Scorer[I, O, E, M]],
    trials: int = 1,
    concurrency: int = 10,
) -> _TaskDecorator[I, O, E, M]: ...


def eval[I, O, E, M](
    *,
    name: str | None = None,
    dataset: Dataset[I, E, M],
    output: TypeForm[O] | Schema[O] | _Unset = _UNSET,
    scorers: Sequence[Scorer[I, O, E, M]],
    trials: int = 1,
    concurrency: int = 10,
) -> _TaskDecorator[I, O, E, M] | _InferredTaskDecorator[I, E, M]:
    def decorate(fn: _InputTask[I, O] | Task[I, O, M]) -> Evaluation[I, O, E, M]:
        annotation = _inferred_output(fn) if isinstance(output, _Unset) else output
        try:
            output_schema = _schema_for(cast("TypeForm[O] | Schema[O]", annotation))
        except Exception as exc:
            if not isinstance(output, _Unset):
                raise
            raise ConfigurationError(
                "Cannot infer output schema; provide explicit output="
            ) from exc
        return Evaluation(
            _definition_name(fn, name),
            dataset,
            output_schema,
            tuple(scorers),
            _contextual_task(fn),
            trials,
            concurrency,
        )

    return decorate
