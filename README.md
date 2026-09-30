# mic

[![CI](https://github.com/rybosome/mic/actions/workflows/ci.yml/badge.svg)](https://github.com/rybosome/mic/actions/workflows/ci.yml)

**Did that prompt, model, or application change actually improve the answers?**

Mic is a small Python evaluation harness for systems whose answers can vary.
Give it examples, the code you want to evaluate, and a way to score the results.
Run the evaluation repeatedly, then inspect what happened, case by case.

Use it to test an LLM classifier, score an agent's work, or evaluate another ML
application. You keep your application code and choose your own models and scoring
logic; Mic handles dataset validation, bounded concurrent execution, repeated
trials, and local evidence. No hosted evaluation platform is required.

## Evaluate a support-ticket classifier

Suppose you're using an LLM to sort support messages into bugs, feature requests,
and questions. Before changing its prompt or model, give yourself a repeatable check.

With Python 3.12+, install Mic and the SDK used by this example:

```console
python -m pip install "mic-evals[pydantic]" openai
```

Mic's core has no third-party runtime dependencies. This example opts into Pydantic
and the OpenAI SDK to share a typed output contract between Mic and the model call.
It uses [structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
with [GPT-4.1 mini](https://developers.openai.com/api/docs/models/gpt-4.1-mini);
replace the classifier function with your own model or application call.
Set `OPENAI_API_KEY` in your environment using your usual secret-management method.
Running it sends the example messages to OpenAI and incurs normal API charges.

Save this complete example as `ticket_eval.py`:

```python
from typing import Literal

from pydantic import BaseModel

import mic


class Ticket(BaseModel):
    subject: str
    body: str


class Classification(BaseModel):
    label: Literal["bug", "feature", "question"]


def classify_ticket(ticket: Ticket) -> Classification:
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


@mic.dataset(name="tickets", schema=mic.case_schema(input=Ticket, expected=Classification))
def tickets() -> list[mic.RawCase]:
    return [
        mic.RawCase(
            id="upload",
            input=Ticket(subject="PDF upload", body="The app closes whenever I upload a PDF."),
            expected=Classification(label="bug"),
        ),
        mic.RawCase(
            id="export",
            input=Ticket(
                subject="Invoice export", body="Can you add an option to export invoices?"
            ),
            expected=Classification(label="feature"),
        ),
        mic.RawCase(
            id="invoice",
            input=Ticket(subject="Past invoice", body="Where can I download last month's invoice?"),
            expected=Classification(label="question"),
        ),
    ]


@mic.scorer(name="accuracy", requires_expected=True)
def accuracy(
    ctx: mic.ScoreContext[Ticket, Classification, Classification, mic.JsonObject],
) -> float:
    return float(ctx.output.label == ctx.require_expected().label)


@mic.eval(name="classify", dataset=tickets, output=Classification, scorers=[accuracy])
def classify(
    ctx: mic.TaskContext[Classification, mic.JsonObject], ticket: Ticket
) -> Classification:
    return classify_ticket(ticket)
```

`Ticket` gives the task typed inputs; `Classification` defines both the expected
answer and the model's structured output. The SDK derives its output schema from
that class and parses the response into it. Mic validates dataset values and task
outputs against the same types, so the scorer works with objects, not JSON parsing
or string cleanup.

A valid but wrong label scores `0`; the right label scores `1`. Invalid output or
no parsed classification (for example, a refusal) is an execution failure, not a
wrong answer. These three cases are illustrative; a useful evaluation needs a
larger, representative set of labeled tickets.

Run it from the directory containing `ticket_eval.py`, then open the report:

```console
mic run ticket_eval:classify --output .mic/tickets-first
mic report .mic/tickets-first --open
```

That's three classifier calls. The report shows each message, its expected label,
the actual response, and its score, alongside aggregate accuracy and execution
failures. Review low-scoring cases for wrong answers or ambiguous expected labels,
and execution failures for calls that did not produce a valid classification.

Each run saves its dataset snapshot, per-trial results, and summary as JSON/JSONL,
plus a self-contained HTML report you can open without a server or network.
Explicit output directories must be empty; omit `--output` to get a unique directory
automatically. A successfully executed run can still have poor scores—execution
success and answer quality are separate.

The same code lives in [examples/ticket_eval.py](examples/ticket_eval.py). To explore
the report without credentials or API charges, the repository also includes an
[offline classification demo](examples/triage.py) with deliberately imperfect rules;
see the [walkthrough](docs/verification.md).

## Repeat, check, improve

### Look for variation

Run each message five times, with at most two tasks executing concurrently:

```console
mic run ticket_eval:classify --trials 5 --concurrency 2 --output .mic/tickets-repeat
```

This makes 15 classifier calls. Inspect individual trials as well as the mean:
one message that fails intermittently deserves attention even if the average looks
good. Repetition gives you more observations, not proof of statistical significance.

Change the prompt or model in `classify_ticket`, run again into a fresh directory,
and review both reports against the same labeled cases. Keep the evaluation set
representative rather than tuning only to these three examples.

### Make quality a check

Add a score requirement when you're ready to use the evaluation locally or in CI:

```console
mic run ticket_eval:classify --trials 5 --require 'accuracy>=0.9'
```

The command exits nonzero if execution fails or mean accuracy falls below `0.9`.
That threshold is illustrative; choose one appropriate to your dataset and the cost
of a wrong answer. A failed quality gate still leaves local results to investigate.

### Grow the dataset without changing the task

Save the same cases as `tickets.jsonl` beside `ticket_eval.py`:

```jsonl
{"id":"upload","input":{"subject":"PDF upload","body":"The app closes whenever I upload a PDF."},"expected":{"label":"bug"}}
{"id":"export","input":{"subject":"Invoice export","body":"Can you add an option to export invoices?"},"expected":{"label":"feature"}}
{"id":"invoice","input":{"subject":"Past invoice","body":"Where can I download last month's invoice?"},"expected":{"label":"question"}}
```

Replace the `tickets` definition with this, adding the two imports:

```python
from pathlib import Path

from mic.providers.files import FileHandle


@mic.dataset(name="tickets", schema=mic.case_schema(input=Ticket, expected=Classification))
def tickets() -> FileHandle:
    return FileHandle(Path(__file__).with_name("tickets.jsonl"))
```

Mic hydrates the JSON objects into `Ticket` and `Classification` instances and
rejects invalid cases before calling the model. The task, scorer, and run command
stay the same. Add cases from real failures as
you encounter them—for example, a message that sounds like a feature request but
describes an existing feature that stopped working.

## Bring your own application

The same pattern applies beyond classification: score extracted fields, check an
agent's result against a rubric, or compute a metric for an ML prediction. Tasks
and scorers are Python functions, so they can call your existing code. Sync and
async functions are supported; scripts can use `mic.run()` and notebooks can use
`await mic.arun()` instead of the CLI.

- **Structured data:** use ordinary dataclasses for inputs, outputs, and expected
  values; see the [structured example](examples/structured.py) and [schema guide](docs/schemas.md).
- **Other dataset sources:** load local files, BigQuery queries, or versioned
  Braintrust datasets with optional integrations; see [providers](docs/providers.md).
- **Remote reporting:** optionally export results to a Braintrust experiment after
  saving local evidence. Dataset storage and reporting are independent; see [reporting](docs/reporting.md).

## Before you use it

Mic is an early release, and its public API may change before a stable release.
It runs finite, bounded datasets locally—not distributed jobs or an application
hosting service. Synchronous callbacks must finish cooperatively: a timeout cannot
forcibly stop a running Python thread.

Reports and artifacts contain your actual evaluation data, including inputs and
outputs. They are **not automatically redacted**. Treat them as sensitive, keep
credentials out of your cases, and review evidence before sharing or committing it.
Your model calls and optional cloud integrations have their own costs and data-handling
policies. See [artifact handling](docs/artifacts.md) for details.

[API reference](docs/api.md) · [Verification](docs/verification.md) ·
[Contributing](CONTRIBUTING.md) · [Releasing](docs/releasing.md) · [MIT license](LICENSE)
