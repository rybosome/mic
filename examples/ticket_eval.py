"""README quickstart. Running this evaluation requires OpenAI credentials and incurs API costs."""

from typing import Literal

from pydantic import BaseModel

import mic


class Ticket(BaseModel):
    subject: str
    body: str


class Classification(BaseModel):
    label: Literal["bug", "feature", "question"]


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
    from openai import OpenAI

    # Create the client only when the task runs, and close it after the call.
    with OpenAI(timeout=30, max_retries=0) as client:
        response = client.responses.parse(
            model="gpt-4.1-mini",
            instructions=(
                "Classify the support ticket as "
                "bug (broken behavior), feature (new capability), or question (how-to)."
            ),
            input=ticket.model_dump_json(),
            text_format=Classification,
            store=False,
        )
        if response.output_parsed is None:
            raise ValueError("The model did not return a classification.")
        return response.output_parsed
