"""Concise definitions preserve schemas, passive discovery, and explicit escape hatches."""

from __future__ import annotations

from collections.abc import Awaitable, Coroutine
from dataclasses import dataclass
from functools import partial, wraps
from inspect import unwrap
from typing import Annotated, Any, TypeVar

import pytest

import mic
from tests.runtime.helpers import cases, scores


@dataclass
class Answer:
    size: int


@mic.dataset(input=str, expected=bool)
def tickets():
    return [mic.RawCase(input="hello", expected=True)]


def test_default_names_and_explicit_overrides(tmp_path):
    @mic.scorer()
    def accuracy(ctx: mic.ScoreContext[str, Answer, bool]) -> float:
        return float(ctx.output.size == 5)

    @mic.eval(dataset=tickets, scorers=[accuracy])
    def classify(ticket: str) -> Answer:
        return Answer(len(ticket))

    assert tickets.name == "tickets"
    assert accuracy.name == "accuracy"
    assert classify.name == "classify"
    assert mic.preflight(classify)["tasks_executed"] == 0
    result = mic.run(classify, output=tmp_path)
    assert result.exit_code == 0
    assert cases(result)[0]["output"] == {"size": 5}
    assert set(result.summary.tasks) == {"classify"}
    assert scores(result)["accuracy"].mean == 1
    assert mic.dataset(name="stable", input=str, expected=bool)(tickets.factory).name == "stable"
    assert mic.scorer(name="stable")(accuracy.function).name == "stable"
    assert (
        mic.eval(name="stable", dataset=tickets, output=Answer, scorers=[])(classify.function).name
        == "stable"
    )


def test_names_without_global_namespace_and_duplicate_metrics(tmp_path):
    def score(ctx):
        return 1

    left = mic.scorer()(score)
    right = mic.scorer()(score)

    @mic.eval(dataset=tickets, scorers=[left, right])
    def task(ticket: str) -> str:
        return ticket

    with pytest.raises(mic.ConfigurationError, match="Duplicate scorer"):
        mic.preflight(task)
    unique = mic.eval(dataset=tickets, scorers=[left, mic.scorer(name="other")(score)])(
        unwrap(task.function)
    )
    assert mic.run(unique, output=tmp_path).exit_code == 0


@pytest.mark.parametrize("kind", ["dataset", "scorer", "eval"])
def test_nameless_callables_require_explicit_name(kind):
    def function(value=None):
        return value

    callback = partial(function)
    kwargs = {"input": str, "expected": str} if kind == "dataset" else {}
    if kind == "eval":
        kwargs = {"dataset": tickets, "output": str, "scorers": []}
    decorator = getattr(mic, kind)
    with pytest.raises(mic.ConfigurationError, match="name="):
        decorator(**kwargs)(callback)
    assert decorator(name="explicit", **kwargs)(callback).name == "explicit"
    assert decorator(name="", **kwargs)(callback).name == ""  # Never replace explicit names.


def test_dataset_forms_are_equivalent_and_keep_metadata_and_mapper():
    schema = mic.case_schema(
        input=str, expected=bool, metadata=dict[str, int], expected_policy="optional"
    )

    def mapper(row):
        return mic.RawCase(input=row, metadata={"length": len(row)})

    def source():
        return ["hello"]

    direct = mic.dataset(
        input=str,
        expected=bool,
        metadata=dict[str, int],
        expected_policy="optional",
        map_row=mapper,
    )(source)
    explicit = mic.dataset(schema=schema, map_row=mapper)(source)
    assert mic.inspect_dataset(direct) == mic.inspect_dataset(explicit)
    assert explicit.schema is schema
    assert direct.map_row is mapper
    assert direct.schema.expected_policy == "optional"
    assert tickets.schema.metadata.validate({"key": 1}) == {"key": 1}


@pytest.mark.parametrize("kwargs", [{}, {"input": str}, {"expected": str}])
def test_incomplete_dataset_definition_fails(kwargs):
    with pytest.raises(mic.ConfigurationError, match="requires"):
        mic.dataset(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [{"input": str}, {"expected": str}, {"metadata": dict}, {"expected_policy": "required"}],
)
def test_schema_and_fields_are_mutually_exclusive(kwargs):
    with pytest.raises(mic.ConfigurationError, match="not both"):
        mic.dataset(schema=tickets.schema, **kwargs)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("contextual", [False, True])
@pytest.mark.parametrize("wrapped", [False, True])
def test_return_forms_are_inferred_without_calling_task(
    tmp_path, asynchronous, contextual, wrapped
):
    calls = []

    def compute(value):
        calls.append(value)
        return (
            mic.TaskResult(Answer(len(value)), {"source": "test"})
            if wrapped
            else Answer(len(value))
        )

    def sync_input(value):
        return compute(value)

    def sync_context(ctx, value):
        assert not hasattr(ctx, "expected")
        return compute(value)

    async def async_input(value):
        return compute(value)

    async def async_context(ctx, value):
        assert not hasattr(ctx, "expected")
        return compute(value)

    fn = (
        (async_context if contextual else async_input)
        if asynchronous
        else (sync_context if contextual else sync_input)
    )
    fn.__annotations__["return"] = "mic.TaskResult[Answer]" if wrapped else "Answer"
    spec = mic.eval(dataset=tickets, scorers=[])(fn)
    assert not calls
    assert mic.preflight(spec)["tasks_executed"] == 0
    assert not calls
    result = mic.run(spec, output=tmp_path)
    assert result.exit_code == 0
    assert cases(result)[0]["output"] == {"size": 5}
    if wrapped:
        assert cases(result)[0]["task_metadata"] == {"source": "test"}


@pytest.mark.parametrize(
    "annotation",
    [Awaitable[Answer], Coroutine[Any, Any, Answer], Awaitable[mic.TaskResult[Answer]]],
)
def test_sync_returning_awaitable(tmp_path, annotation):
    async def compute():
        return mic.TaskResult(Answer(5)) if "TaskResult" in str(annotation) else Answer(5)

    def task(value):
        return compute()

    task.__annotations__["return"] = annotation
    assert mic.run(mic.eval(dataset=tickets, scorers=[])(task), output=tmp_path).exit_code == 0


@pytest.mark.parametrize(
    "annotation,value",
    [
        (int | None, None),
        (mic.TaskResult[int] | int, mic.TaskResult(3)),
        (list[int], [1, 2]),
        (None, None),
    ],
)
def test_union_container_and_null_outputs(tmp_path, annotation, value):
    def task(ticket):
        return value

    task.__annotations__["return"] = annotation
    result = mic.run(mic.eval(dataset=tickets, scorers=[])(task), output=tmp_path)
    assert result.exit_code == 0


@pytest.mark.parametrize(
    "annotation",
    [
        "DoesNotExist",
        Any,
        object,
        TypeVar("T"),
        Annotated[int, "constraint"],
        Awaitable,
        mic.TaskResult,
    ],
)
def test_unusable_annotations_fail_without_probing(annotation):
    def task(value):
        raise AssertionError("Do not execute tasks during definition")

    task.__annotations__["return"] = annotation
    with pytest.raises(mic.ConfigurationError, match="output="):
        mic.eval(dataset=tickets, scorers=[])(task)


def test_missing_return_annotation_and_explicit_override(tmp_path):
    def task(value):
        return 5

    with pytest.raises(mic.ConfigurationError, match="output="):
        mic.eval(dataset=tickets, scorers=[])(task)
    task.__annotations__ = {"value": "UnknownInput", "return": "UnknownOutput"}
    explicit = mic.eval(dataset=tickets, output=mic.schema(int), scorers=[])(task)
    assert mic.run(explicit, output=tmp_path).exit_code == 0


def test_only_return_annotation_is_resolved_and_wrapped_function_works(tmp_path):
    def task(value) -> Answer:
        return Answer(len(value))

    task.__annotations__["value"] = "UnknownInput"

    @wraps(task)
    def wrapper(*args, **kwargs):
        return task(*args, **kwargs)

    assert mic.run(mic.eval(dataset=tickets, scorers=[])(wrapper), output=tmp_path).exit_code == 0


def test_bad_return_is_still_validated(tmp_path):
    @mic.eval(dataset=tickets, scorers=[])
    def task(value: str) -> int:
        return "wrong"

    result = mic.run(task, output=tmp_path)
    assert result.exit_code == 1
    assert cases(result)[0]["errors"][0]["phase"] == "schema"


def test_local_forward_reference_requires_explicit_output():
    @dataclass
    class Local:
        value: int

    def task(value) -> Local:
        return Local(1)

    with pytest.raises(mic.ConfigurationError, match="output="):
        mic.eval(dataset=tickets, scorers=[])(task)
    assert mic.eval(dataset=tickets, output=Local, scorers=[])(task).output.validate(
        {"value": 1}
    ) == Local(1)
