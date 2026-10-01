"""Concise authoring retains explicit dispatch, types, and failure semantics."""

import asyncio
import inspect
import threading
from dataclasses import dataclass
from functools import partial, wraps
from typing import get_args

import pytest

import mic


@mic.dataset(name="numbers", schema=mic.case_schema(input=int, expected=int))
def numbers():
    return [mic.RawCase(input=2, expected=4)]


def define(fn):
    return mic.eval(name="double", dataset=numbers, output=int, scorers=[])(fn)


def test_context_default_arguments_and_explicit_overrides():
    @dataclass
    class Metadata:
        segment: str

    assert get_args(mic.TaskContext[int]) == (int, mic.JsonObject)
    assert get_args(mic.TaskContext[int, Metadata]) == (int, Metadata)
    assert get_args(mic.ScoreContext[str, int]) == (str, int, int, mic.JsonObject)
    assert get_args(mic.ScoreContext[str, int, bool]) == (str, int, bool, mic.JsonObject)
    assert get_args(mic.ScoreContext[str, int, bool, Metadata]) == (str, int, bool, Metadata)
    assert mic.TaskContext[int]("a", 1, 4, {}).require_expected() == 4
    assert mic.ScoreContext[str, int]("two", 4, 4, {}).output == 4
    with pytest.raises(TypeError):
        mic.ScoreContext[int]
    with pytest.raises(TypeError):
        mic.TaskContext[int, int, int]


@pytest.mark.parametrize("contextual", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("wrapped_result", [False, True])
async def test_task_forms_preserve_dispatch_and_result_metadata(
    tmp_path, contextual, asynchronous, wrapped_result
):
    owner = threading.get_ident()
    threads = []

    def answer(value):
        threads.append(threading.get_ident())
        return mic.TaskResult(value * 2, {"source": "task"}) if wrapped_result else value * 2

    def input_only(value, /, *, unused=True):
        return answer(value)

    def with_context(context, value, /, *, unused=True):
        assert context.require_expected() == 4
        return answer(value)

    async def async_input(value):
        await asyncio.sleep(0)
        return answer(value)

    async def async_context(context, value):
        assert context.require_expected() == 4
        return await async_input(value)

    fn = (
        (async_context if contextual else async_input)
        if asynchronous
        else (with_context if contextual else input_only)
    )
    spec = define(fn)
    assert not threads  # Definition never probes by calling the function.
    assert inspect.iscoroutinefunction(spec.function) == asynchronous
    assert spec.function.__module__ == fn.__module__
    result = await mic.arun(spec, output=tmp_path)
    assert result.exit_code == 0
    assert result.cases[0]["output"] == 4
    assert (threads == [owner]) == asynchronous
    if wrapped_result:
        assert result.cases[0]["task_metadata"] == {"source": "task"}


@pytest.mark.parametrize("asynchronous", [False, True])
def test_type_error_is_not_retried(tmp_path, asynchronous):
    calls = []

    def task(value):
        calls.append(value)
        raise TypeError("inside task")

    async def async_task(value):
        return task(value)

    result = mic.run(define(async_task if asynchronous else task), output=tmp_path)
    assert result.exit_code == 1
    assert calls == [2]
    assert result.cases[0]["errors"][0]["phase"] == "task"


def test_signature_is_selected_once_not_by_parameter_names(tmp_path, monkeypatch):
    inspected = []
    signature = inspect.signature

    def fn(first, second):
        assert isinstance(first, mic.TaskContext)
        return second * 2

    def record(callback, **kwargs):
        if callback is fn:
            inspected.append(callback)
        return signature(callback, **kwargs)

    monkeypatch.setattr(inspect, "signature", record)
    spec = define(fn)
    assert inspected == [fn]
    assert mic.run(spec, trials=3, output=tmp_path).exit_code == 0
    assert inspected == [fn]


@pytest.mark.parametrize(
    "fn",
    [
        lambda: 1,
        lambda a, b, c: 1,
        lambda *args: 1,
        lambda value, **kwargs: 1,
        lambda *, value: 1,
        lambda value, *, required: 1,
    ],
)
def test_unsupported_signatures_fail_at_definition(fn):
    with pytest.raises(mic.ConfigurationError, match="Task must accept"):
        define(fn)


def test_uninspectable_callback_fails_at_definition():
    def task(value):
        return value

    task.__signature__ = "not a signature"
    with pytest.raises(mic.ConfigurationError, match="inspectable signature"):
        define(task)


def test_bound_partial_and_decorated_functions(tmp_path):
    class Multiplier:
        def double(self, value):
            return value * 2

    def multiply(value, *, factor):
        return value * factor

    @wraps(Multiplier().double)
    def decorated(*args, **kwargs):
        return Multiplier().double(*args, **kwargs)

    for index, fn in enumerate((Multiplier().double, partial(multiply, factor=2), decorated)):
        assert mic.run(define(fn), output=tmp_path / str(index)).cases[0]["output"] == 4


async def test_sync_function_returning_awaitable(tmp_path):
    async def double(value):
        return value * 2

    def task(value):
        return double(value)

    result = await mic.arun(define(task), output=tmp_path)
    assert result.exit_code == 0
    assert result.cases[0]["output"] == 4
