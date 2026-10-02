"""Inference retains exact types, including scorer-free evaluations."""

from collections.abc import Awaitable
from typing import assert_type

import mic


@mic.dataset(input=str, expected=bool)
def rows() -> list[mic.RawCase]:
    return [mic.RawCase(input="ticket", expected=True)]


@mic.scorer()
def length(ctx: mic.ScoreContext[str, int, bool]) -> float:
    return float(ctx.output)


@mic.eval(dataset=rows, scorers=[length])
def measured(value: str) -> int:
    return len(value)


@mic.eval(dataset=rows, scorers=[])
def unscored(value: str) -> int:
    return len(value)


@mic.eval(dataset=rows, scorers=[])
async def async_unscored(value: str) -> mic.TaskResult[int]:
    return mic.TaskResult(len(value))


@mic.eval(dataset=rows, scorers=[])
def contextual(ctx: mic.TaskContext[bool], value: str) -> int:
    return len(value) if ctx.require_expected() else 0


@mic.eval(dataset=rows, scorers=[length])
async def async_measured(value: str) -> mic.TaskResult[int]:
    return mic.TaskResult(len(value))


async def compute(value: str) -> int:
    return len(value)


@mic.eval(dataset=rows, scorers=[])
def awaitable(value: str) -> Awaitable[int]:
    return compute(value)


@mic.dataset(input=str, expected=bool, metadata=dict[str, int], expected_policy="optional")
def metadata_rows() -> list[mic.RawCase]:
    return []


assert_type(rows, mic.Dataset[str, bool, mic.JsonObject])
assert_type(metadata_rows, mic.Dataset[str, bool, dict[str, int]])
assert_type(measured, mic.Evaluation[str, int, bool, mic.JsonObject])
assert_type(unscored, mic.Evaluation[str, int, bool, mic.JsonObject])
assert_type(async_unscored, mic.Evaluation[str, int, bool, mic.JsonObject])
assert_type(contextual, mic.Evaluation[str, int, bool, mic.JsonObject])
assert_type(async_measured, mic.Evaluation[str, int, bool, mic.JsonObject])
assert_type(awaitable, mic.Evaluation[str, int, bool, mic.JsonObject])


@mic.eval(dataset=rows, scorers=[length])  # expect-error
def wrong_output(value: str) -> str:
    return value


@mic.eval(dataset=rows, scorers=[])  # expect-error
def wrong_input(value: int) -> int:
    return value


@mic.eval(dataset=rows, scorers=[])  # expect-error
def wrong_context(ctx: mic.TaskContext[str], value: str) -> int:
    return len(value)


mic.dataset(input=str)  # expect-error
mic.dataset(  # expect-error
    schema=mic.case_schema(input=str, expected=str), input=str, expected=str
)
