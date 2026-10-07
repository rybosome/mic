"""README quickstart. Running this evaluation requires TypeSafe credentials and incurs API costs."""

from dataclasses import dataclass
from typing import Literal, cast

import mic


@dataclass
class Ticket:
    subject: str
    body: str


Label = Literal["bug", "feature", "question"]


@dataclass
class Classification:
    label: Label


@mic.dataset(input=Ticket, expected=Classification)
def tickets() -> list[tuple[Ticket, Classification]]:
    """Return a static list of support tickets and expected classifications."""
    return [
        (
            Ticket(subject="PDF upload", body="The app closes whenever I upload a PDF."),
            Classification(label="bug"),
        ),
        (
            Ticket(subject="Invoice export", body="Can you add an option to export invoices?"),
            Classification(label="feature"),
        ),
        (
            Ticket(subject="Past invoice", body="Where can I download last month's invoice?"),
            Classification(label="question"),
        ),
    ]


@mic.scorer()
def accuracy(ctx: mic.ScoreContext[Ticket, Classification]) -> float:
    """Binary scorer grading actual label matching expected label."""
    return float(ctx.output.label == ctx.require_expected().label)


@mic.eval(dataset=tickets, scorers=[accuracy])
def classify(ticket: Ticket) -> Classification:
    """Call Jev and classify the given ticket."""
    from typesafe_sdk import Choice, RetryPolicy, TypeSafeClient

    with TypeSafeClient(timeout=30, retry=RetryPolicy(max_retries=0)) as client:
        response = client.system_one(
            model="jev-latest",
            state={
                "instructions": "Classify the support ticket using the label descriptions.",
                "message": {"subject": ticket.subject, "body": ticket.body},
            },
            questions={
                "label": Choice(
                    instructions="Which classification label applies to this ticket?",
                    criteria={
                        "bug": "Broken behavior or an error in an existing capability.",
                        "feature": "A request for a new capability or enhancement.",
                        "question": "A request for information or how-to help.",
                    },
                ),
            },
        )
        label = response.choices["label"].choice
        if label not in ("bug", "feature", "question"):
            raise ValueError("The model did not return an allowed classification.")
        return Classification(label=cast(Label, label))
