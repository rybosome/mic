"""Nested stdlib dataclasses from native Python values or the same JSONL rows.

Run ``mic run examples.structured:classify`` or select ``classify_file``.
This example requires no third-party packages.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import mic
from mic.providers.files import FileHandle


@dataclass(frozen=True, slots=True)
class Message:
    role: Literal["user", "assistant"]
    text: str


@dataclass
class Ticket:
    messages: list[Message]
    history: dict[str, list[Message]] = field(default_factory=lambda: {})


@dataclass
class Decision:
    label: Literal["bug", "feature", "question"]
    evidence: dict[str, list[int]] = field(default_factory=lambda: {})
    owner: str | None = None


@dataclass
class Metadata:
    segment: str = "demo"
    model: str | None = None


case_types = mic.case_schema(input=Ticket, expected=Decision, metadata=Metadata)


@mic.dataset(name="structured.memory", schema=case_types)
def tickets() -> list[mic.RawCase]:
    return [
        mic.RawCase(
            id="crash",
            input=Ticket(
                messages=[Message("user", "It crashes on startup")],
                history={"support": [Message("assistant", "Which version?")]},
            ),
            expected=Decision("bug", evidence={"messages": [0]}),
            metadata=Metadata(),
        ),
        mic.RawCase(
            id="feature",
            input=Ticket(messages=[Message("user", "Please add dark mode")]),
            expected=Decision("feature", evidence={"messages": [0]}),
            metadata=Metadata(),
        ),
    ]


@mic.dataset(name="structured.file", schema=case_types)
def file_tickets() -> FileHandle:
    return FileHandle(Path(__file__).parent / "fixtures" / "structured.jsonl")


@mic.scorer(name="decision", metrics=("label", "evidence"))
def decision_scores(ctx: mic.ScoreContext[Ticket, Decision, Decision, Metadata]) -> list[mic.Score]:
    expected = ctx.require_expected()
    return [
        mic.Score("label", float(ctx.output.label == expected.label)),
        mic.Score("evidence", float(ctx.output.evidence == expected.evidence)),
    ]


def classify_ticket(ticket: Ticket) -> mic.TaskResult[Decision]:
    for index, message in enumerate(ticket.messages):
        if message.role != "user":
            continue
        text = message.text.lower()
        if "crash" in text:
            return mic.TaskResult(
                Decision("bug", evidence={"messages": [index]}), metadata={"model": "rules"}
            )
        if "add" in text:
            return mic.TaskResult(
                Decision("feature", evidence={"messages": [index]}), metadata={"model": "rules"}
            )
    return mic.TaskResult(Decision("question"), metadata={"model": "rules"})


@mic.eval(name="structured.classify", dataset=tickets, output=Decision, scorers=[decision_scores])
def classify(ctx: mic.TaskContext[Decision, Metadata], ticket: Ticket) -> mic.TaskResult[Decision]:
    return classify_ticket(ticket)


@mic.eval(
    name="structured.classify_file",
    dataset=file_tickets,
    output=Decision,
    scorers=[decision_scores],
)
def classify_file(
    ctx: mic.TaskContext[Decision, Metadata], ticket: Ticket
) -> mic.TaskResult[Decision]:
    return classify_ticket(ticket)
