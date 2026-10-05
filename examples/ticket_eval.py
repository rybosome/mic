"""README quickstart. Running this evaluation requires TypeSafe credentials and incurs API costs."""

from dataclasses import dataclass
from typing import Literal, cast, get_args, get_type_hints

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
    return float(ctx.output.label == ctx.require_expected().label)


@mic.eval(dataset=tickets, scorers=[accuracy])
def classify(ticket: Ticket) -> Classification:
    from typesafe_sdk import Choice, RetryPolicy, TypeSafeClient

    labels = get_args(get_type_hints(Classification)["label"])
    # Create the client only when the task runs, and close it after the call.
    with TypeSafeClient(timeout=30, retry=RetryPolicy(max_retries=0)) as client:
        response = client.system_one(
            model="jev-latest",
            state={
                "instructions": (
                    "Classify support tickets: bug means broken behavior, "
                    "feature means a new capability, and question means how-to help."
                ),
                "message": {"subject": ticket.subject, "body": ticket.body},
            },
            questions={
                "label": Choice(
                    instructions="Which classification label applies to this ticket?",
                    criteria={label: None for label in labels},
                ),
            },
        )
        label = response.choices["label"].choice
        if label not in labels:
            raise ValueError("The model did not return an allowed classification.")
        return Classification(label=cast(Label, label))
