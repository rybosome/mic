"""Intentional failure and null-score demos for inspecting truthful local evidence."""

from typing import cast

import mic
from examples.triage import exact, triage_data
from mic import JsonObject, Missing, Score, ScoreContext, TaskContext


@mic.eval(name="failures.task", dataset=triage_data, output=str, scorers=[exact])
def task_error(context: TaskContext[str, JsonObject], text: str) -> str:
    if "crashes" in text:
        raise RuntimeError("Demonstration task error: unavailable upstream service")
    return context.require_expected()


@mic.eval(name="failures.output_schema", dataset=triage_data, output=str, scorers=[exact])
def output_schema(context: TaskContext[str, JsonObject], text: str) -> str:
    # This deliberate annotation violation demonstrates runtime validation of external values.
    return cast(str, 17) if "crashes" in text else context.require_expected()


@mic.scorer(name="unstable")
def unstable(context: ScoreContext[str, str, str, JsonObject]) -> Score:
    raise ValueError("Demonstration scorer error")


@mic.eval(name="failures.scorer", dataset=triage_data, output=str, scorers=[exact, unstable])
def scorer_error(context: TaskContext[str, JsonObject], text: str) -> str:
    return context.require_expected()


@mic.dataset(
    name="nullable.expected",
    schema=mic.case_schema(input=str, expected=mic.schema(str | None), expected_policy="optional"),
)
def nullable_data() -> list[object]:
    return [
        {"id": "missing", "input": "no label"},
        {"id": "null", "input": "explicit null", "expected": None},
        {"id": "labeled", "input": "known label", "expected": "value"},
    ]


@mic.scorer(name="nullable_exact", requires_expected=False)
def nullable_exact(context: ScoreContext[str, str, str | None, JsonObject]) -> Score:
    if isinstance(context.expected, Missing) or context.expected is None:
        return Score("nullable_exact", None, metadata={"reason": "No usable label"})
    return Score("nullable_exact", float(context.output == context.expected))


@mic.eval(name="failures.null_score", dataset=nullable_data, output=str, scorers=[nullable_exact])
def null_score(context: TaskContext[str | None, JsonObject], text: str) -> str:
    return "value"
