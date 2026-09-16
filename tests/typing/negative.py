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
def wrong_scorer(ctx: mic.ScoreContext[int, str, str, mic.JsonObject]) -> mic.Score:
    return mic.Score("wrong-scorer", 1)


mic.eval(
    name="wrong-scorer-input",
    dataset=text_data,
    output=str,
    scorers=[wrong_scorer],  # expect-error
)


def absent_expected_is_not_a_string(ctx: mic.ScoreContext[str, str, str, mic.JsonObject]) -> str:
    return ctx.expected  # expect-error
