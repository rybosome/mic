"""Nested dataclasses retain their complete types through the public API."""

from typing import assert_type

import mic
from examples.structured import Decision, Message, Metadata, Ticket, classify, tickets

assert_type(tickets, mic.Dataset[Ticket, Decision, Metadata])
assert_type(classify, mic.Evaluation[Ticket, Decision, Decision, Metadata])
assert_type(mic.schema(list[Message]), mic.Schema[list[Message]])
assert_type(mic.schema(dict[str, list[Message]]), mic.Schema[dict[str, list[Message]]])
assert_type(mic.schema(str | None), mic.Schema[str | None])


def nested_values(context: mic.ScoreContext[Ticket, Decision, Decision, Metadata]) -> None:
    assert_type(context.input.messages[0], Message)
    assert_type(context.input.history["support"], list[Message])
    assert_type(context.output.evidence, dict[str, list[int]])
    assert_type(context.require_expected(), Decision)
    context.output.evidence["messages"] = ["bad"]  # expect-error


@mic.eval(  # expect-error
    name="wrong-structured-input", dataset=tickets, output=Decision, scorers=[]
)
def wrong_input(ctx: mic.TaskContext[Metadata], ticket: str) -> Decision:
    return Decision("bug")
