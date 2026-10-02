# 🎤 mic

[![CI](https://github.com/rybosome/mic/actions/workflows/ci.yml/badge.svg)](https://github.com/rybosome/mic/actions/workflows/ci.yml)

Typed evaluations for LLMs, agents, and other nondeterministic systems. Define
your cases, scoring functions, and application call; Mic runs repeated trials
and saves inspectable results locally.

## Install

Requires **Python 3.12+**. The core has no third-party runtime dependencies.

```console
uv venv --python 3.12
source .venv/bin/activate
uv pip install mic-evals
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1`.
Already using a virtual environment? Skip its creation and activation.
You can substitute `python -m pip install` for `uv pip install`.

## Quickstart: evaluate a support-ticket classifier

An evaluation has three parts: **cases to test**, **what counts as a good answer**,
and **the code to call**. This example classifies tickets as bugs, feature requests,
or questions.

```console
uv pip install 'mic-evals[pydantic]' openai
```

Set `OPENAI_API_KEY` in your environment. Running this example sends tickets to
OpenAI and incurs API charges. Save the following **three Python blocks together**
as `ticket_eval.py` ([complete file](examples/ticket_eval.py)).

### 1. Define the dataset

Each case pairs a structured input with an expected answer.

<!-- snippet: quickstart-dataset -->
```python
from typing import Literal

from pydantic import BaseModel

import mic


class Ticket(BaseModel):
    subject: str
    body: str


class Classification(BaseModel):
    label: Literal["bug", "feature", "question"]


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
```

### 2. Define the scoring

Give a correct label `1`, an incorrect label `0`.

<!-- snippet: quickstart-scoring -->
```python
@mic.scorer(name="accuracy")
def accuracy(ctx: mic.ScoreContext[Ticket, Classification]) -> float:
    return float(ctx.output.label == ctx.require_expected().label)
```

### 3. Define the task

Call your application and return its result. Here, the
[OpenAI structured-output parser](https://developers.openai.com/api/docs/guides/structured-outputs)
uses the same `Classification` model as Mic.

<!-- snippet: quickstart-task -->
```python
@mic.eval(name="classify", dataset=tickets, output=Classification, scorers=[accuracy])
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
```

### Run and inspect

```console
mic run ticket_eval:classify --output .mic/tickets
mic report .mic/tickets --open
```

See each ticket, expected answer, prediction, and score in a standalone HTML report;
JSON/JSONL evidence is saved alongside it. Explicit output directories must be empty.
Omit `--output` to create a unique one. These three cases are illustrative, not a
representative benchmark.

## Datasets: keep the evaluation, change the source

### Dataclasses or Pydantic

Mic supports ordinary dataclasses without extra dependencies:

<!-- snippet: dataclasses -->
```python
from dataclasses import dataclass
from typing import Literal


@dataclass
class Ticket:
    subject: str
    body: str


@dataclass
class Classification:
    label: Literal["bug", "feature", "question"]
```

The quickstart uses optional Pydantic models to share an output schema with OpenAI.
Both work with Mic's dataset and output schemas; the SDK-specific call above uses
Pydantic's `model_dump_json()`. See [structured schemas](docs/schemas.md).

### Local JSONL

Save these records as `tickets.jsonl` beside `ticket_eval.py`:

<!-- snippet: tickets-jsonl -->
```jsonl
{"id":"upload","input":{"subject":"PDF upload","body":"The app closes whenever I upload a PDF."},"expected":{"label":"bug"}}
{"id":"export","input":{"subject":"Invoice export","body":"Can you add an option to export invoices?"},"expected":{"label":"feature"}}
{"id":"invoice","input":{"subject":"Past invoice","body":"Where can I download last month's invoice?"},"expected":{"label":"question"}}
```

Replace the quickstart's `tickets` factory and add these imports:

<!-- snippet: file-dataset -->
```python
from pathlib import Path

from mic.providers.files import FileHandle


@mic.dataset(name="tickets", schema=mic.case_schema(input=Ticket, expected=Classification))
def tickets() -> FileHandle:
    return FileHandle(Path(__file__).with_name("tickets.jsonl"))
```

Mic hydrates the JSON objects into your declared types and validates cases before
running the task. The scorer, task, and run commands do not change.

### BigQuery

```console
uv pip install 'mic-evals[bigquery]'
```

Configure Application Default Credentials. Replace the dataset factory with this
one, using your billing project, location, and table (columns: `id`, `subject`,
`body`, `label`):

<!-- snippet: bigquery-dataset -->
```python
from mic.providers.bigquery import BigQueryHandle


@mic.dataset(name="tickets", schema=mic.case_schema(input=Ticket, expected=Classification))
def tickets() -> BigQueryHandle:
    return BigQueryHandle(
        billing_project="your-project",
        location="US",
        maximum_bytes_billed=10_000_000,
        sql="""
            SELECT id, STRUCT(subject, body) AS input, STRUCT(label) AS expected
            FROM `your-project.evals.tickets`
            ORDER BY id
        """,
    )
```

### Braintrust

```console
uv pip install 'mic-evals[braintrust]'
```

Set `BRAINTRUST_API_KEY`. Use an existing dataset with the same `input` and
`expected` objects as the JSONL above, pinned to an explicit version:

<!-- snippet: braintrust-dataset -->
```python
from mic.providers.braintrust import BraintrustHandle


@mic.dataset(name="tickets", schema=mic.case_schema(input=Ticket, expected=Classification))
def tickets() -> BraintrustHandle:
    return BraintrustHandle(
        dataset_id="your-dataset-id",
        xact_id="your-pinned-version",
    )
```

Factories describe sources; reading a cloud dataset contacts that service.
See [provider setup, limits, and custom sources](docs/providers.md).

### Custom

A dataset factory can return a fresh iterable or async iterable of `mic.RawCase`
objects—no provider interface needed. For a reusable integration, return a passive
handle instead. Handles have no required base class or methods; their registered
loader implements these interfaces from `mic.providers.base`:

<!-- snippet: custom-provider-contracts -->
```python
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from mic import JsonObject, ReadLimits


class DatasetRead(Protocol):
    @property
    def provenance(self) -> JsonObject: ...

    def rows(self) -> AsyncIterator[object]: ...


class DatasetLoader[H](Protocol):
    def open(
        self, handle: H, *, limits: ReadLimits
    ) -> AbstractAsyncContextManager[DatasetRead]: ...
```

`rows()` yields `RawCase` objects or the same row mappings shown in the JSONL
example. Open resources inside the async context manager and close them on exit;
honor read limits and keep credentials out of provenance.

Create a `Resolver.with_builtin_loaders()` from `mic.providers.base`, register
your handle and loader with `resolver.register(TicketHandle, TicketLoader())`,
then pass it to `mic.run(classify, resolver=resolver)` (or `mic.arun`). The
`tickets` factory returns your `TicketHandle`; the scorer and task stay unchanged.
Custom resolver registration is programmatic, not a CLI configuration option.

## Scoring: multiple metrics and supporting evidence

Mic does not bundle built-in scorers. Each scorer produces **one named metric**;
attach multiple scorers to an evaluation. Return a number, `mic.Score` with JSON
metadata, or `None` when a metric does not apply.

Replace the quickstart's scorer block with:

<!-- snippet: extended-scoring -->
```python
@mic.scorer(name="accuracy")
def accuracy(ctx: mic.ScoreContext[Ticket, Classification]) -> mic.Score:
    expected = ctx.require_expected().label
    return mic.Score(
        value=float(ctx.output.label == expected),
        metadata={"body_length": len(ctx.input.body)},
    )


@mic.scorer(name="bug_recall")
def bug_recall(ctx: mic.ScoreContext[Ticket, Classification]) -> float | None:
    if ctx.require_expected().label != "bug":
        return None
    return float(ctx.output.label == "bug")
```

Then change the task's decorator to:

<!-- snippet: multiple-scorers -->
```python
@mic.eval(name="classify", dataset=tickets, output=Classification, scorers=[accuracy, bug_recall])
```

Overall accuracy can hide missed bugs. `bug_recall` measures the fraction of labeled
bugs found; its mean excludes the non-bug cases that return `None`.
Reference-free metrics use `@mic.scorer(name=..., requires_expected=False)`.
See [scoring contracts](docs/api.md).

## Run regularly

```console
# Inspect cases; cloud sources perform reads.
mic inspect ticket_eval:tickets --limit 3

# Validate the dataset and configuration without calling the task.
mic preflight ticket_eval:classify

# Five trials per case, at most two tasks running concurrently.
mic run ticket_eval:classify --trials 5 --concurrency 2

# Exit nonzero on execution failure or mean accuracy below this example threshold.
mic run ticket_eval:classify --require 'accuracy>=0.9'
```

Change the prompt or model, rerun, and inspect which cases improved or regressed.
Tasks and scorers can be sync or async. Use `mic.run()` in scripts or
`await mic.arun()` in notebooks; request `(ctx, input)` only when your task needs
execution context. [API reference](docs/api.md)

## Notes and documentation

- **Early release:** the API may change before a stable release.
- **Sensitive evidence:** inputs, outputs, and errors are not automatically redacted.
  Review artifacts before sharing them.

[Artifacts](docs/artifacts.md) · [Optional Braintrust reporting](docs/reporting.md) ·
[Verification and offline demo](docs/verification.md) ·
[Contributing](CONTRIBUTING.md) · [Releasing](docs/releasing.md) · [MIT license](LICENSE)
