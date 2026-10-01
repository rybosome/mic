"""Authoring patterns that must retain their full generic types under Pyright strict."""

from typing import assert_type

import mic


@mic.dataset(name="text", schema=mic.case_schema(input=str, expected=str))
def text_data() -> list[mic.RawCase]:
    return [mic.RawCase(input="ping", expected="PING")]


@mic.scorer(name="exact")
def exact(ctx: mic.ScoreContext[str, str, str, mic.JsonObject]) -> float:
    assert_type(ctx.input, str)
    assert_type(ctx.output, str)
    assert_type(ctx.require_expected(), str)
    return float(ctx.output == ctx.require_expected())


@mic.eval(name="sync", dataset=text_data, output=str, scorers=[exact])
def sync_eval(ctx: mic.TaskContext[str, mic.JsonObject], value: str) -> str:
    assert_type(ctx.require_expected(), str)
    return value.upper()


@mic.eval(name="async", dataset=text_data, output=str, scorers=[exact])
async def async_eval(ctx: mic.TaskContext[str, mic.JsonObject], value: str) -> mic.TaskResult[str]:
    return mic.TaskResult(value.upper(), {"case_id": ctx.case_id})


assert_type(text_data, mic.Dataset[str, str, mic.JsonObject])
assert_type(exact, mic.Scorer[str, str, str, mic.JsonObject])
assert_type(sync_eval, mic.Evaluation[str, str, str, mic.JsonObject])
assert_type(async_eval, mic.Evaluation[str, str, str, mic.JsonObject])


@mic.scorer(name="concise")
def concise(ctx: mic.ScoreContext[str, str]) -> float:
    assert_type(ctx.input, str)
    assert_type(ctx.output, str)
    assert_type(ctx.require_expected(), str)
    assert_type(ctx.metadata, mic.JsonObject | None)
    return float(ctx.output == ctx.require_expected())


@mic.eval(name="input-only", dataset=text_data, output=str, scorers=[concise])
def input_only(value: str, /, *, suffix: str = "!") -> str:
    return value.upper() + suffix


@mic.eval(name="async-input-only", dataset=text_data, output=str, scorers=[concise])
async def async_input_only(value: str) -> mic.TaskResult[str]:
    return mic.TaskResult(value.upper())


@mic.eval(name="concise-context", dataset=text_data, output=str, scorers=[concise])
def concise_context(ctx: mic.TaskContext[str], value: str) -> str:
    assert_type(ctx.require_expected(), str)
    assert_type(ctx.metadata, mic.JsonObject | None)
    return value


def distinct_expected(ctx: mic.ScoreContext[str, int, bool]) -> None:
    assert_type(ctx.input, str)
    assert_type(ctx.output, int)
    assert_type(ctx.require_expected(), bool)
    assert_type(ctx.metadata, mic.JsonObject | None)


assert_type(concise, mic.Scorer[str, str, str, mic.JsonObject])
assert_type(input_only, mic.Evaluation[str, str, str, mic.JsonObject])
assert_type(async_input_only, mic.Evaluation[str, str, str, mic.JsonObject])
assert_type(concise_context, mic.Evaluation[str, str, str, mic.JsonObject])
