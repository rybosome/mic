"""README quickstart. Running this evaluation requires OpenAI credentials and incurs API costs."""

import mic


def classify_ticket(message: str) -> str:
    from openai import OpenAI

    # Create the client only when the task runs, and close it after the call.
    with OpenAI(timeout=30, max_retries=0) as client:
        response = client.responses.create(
            model="gpt-4.1-mini",
            instructions=(
                "Classify the support message. Reply with only one label: "
                "bug (broken behavior), feature (new capability), or question (how-to)."
            ),
            input=message,
            store=False,
        )
        return response.output_text.strip()


@mic.dataset(name="tickets", schema=mic.case_schema(input=str, expected=str))
def tickets() -> list[mic.RawCase]:
    return [
        mic.RawCase(id="upload", input="The app closes whenever I upload a PDF.", expected="bug"),
        mic.RawCase(
            id="export", input="Can you add an option to export invoices?", expected="feature"
        ),
        mic.RawCase(
            id="invoice", input="Where can I download last month's invoice?", expected="question"
        ),
    ]


@mic.scorer(name="accuracy", requires_expected=True)
def accuracy(ctx: mic.ScoreContext[str, str, str, mic.JsonObject]) -> float:
    return float(ctx.output == ctx.require_expected())


@mic.eval(name="classify", dataset=tickets, output=str, scorers=[accuracy])
def classify(ctx: mic.TaskContext[str, mic.JsonObject], message: str) -> str:
    return classify_ticket(message)
