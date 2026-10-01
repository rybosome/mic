"""Intentional authoring mistakes; verify_typing.py requires errors on the marked lines."""

import mic
from tests.typing.positive import exact, text_data


@mic.eval(name="wrong-input", dataset=text_data, output=str, scorers=[exact])  # expect-error
def wrong_input(ctx: mic.TaskContext[str, mic.JsonObject], value: int) -> str:
    return str(value)


@mic.eval(name="wrong-return", dataset=text_data, output=str, scorers=[exact])  # expect-error
def wrong_return(ctx: mic.TaskContext[str, mic.JsonObject], value: str) -> int:
    return len(value)


@mic.eval(name="wrong-expected", dataset=text_data, output=str, scorers=[exact])  # expect-error
def wrong_expected(ctx: mic.TaskContext[int, mic.JsonObject], value: str) -> str:
    return value


@mic.scorer(name="wrong-scorer")
def wrong_scorer(ctx: mic.ScoreContext[int, str, str, mic.JsonObject]) -> float:
    return {"wrong": "scorer"}  # expect-error


mic.eval(
    name="wrong-scorer-input",
    dataset=text_data,
    output=str,
    scorers=[wrong_scorer],  # expect-error
)


def absent_expected_is_not_a_string(ctx: mic.ScoreContext[str, str, str, mic.JsonObject]) -> str:
    return ctx.expected  # expect-error


@mic.eval(name="wrong-input-only", dataset=text_data, output=str, scorers=[exact])  # expect-error
def wrong_input_only(value: int) -> str:
    return str(value)


@mic.eval(name="wrong-output-only", dataset=text_data, output=str, scorers=[exact])  # expect-error
def wrong_output_only(value: str) -> int:
    return len(value)


@mic.eval(name="wrong-async-output", dataset=text_data, output=str, scorers=[exact])  # expect-error
async def wrong_async_output(value: str) -> mic.TaskResult[int]:
    return mic.TaskResult(len(value))


@mic.eval(  # expect-error
    name="wrong-context-default", dataset=text_data, output=str, scorers=[exact]
)
def wrong_context_default(ctx: mic.TaskContext[int], value: str) -> str:
    return value


def wrong_default_access(ctx: mic.ScoreContext[str, int]) -> str:
    return ctx.require_expected()  # expect-error


def wrong_metadata_access(ctx: mic.TaskContext[str]) -> str:
    return ctx.metadata  # expect-error
