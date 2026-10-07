# 🎤 mic

[![CI](https://github.com/rybosome/mic/actions/workflows/ci.yml/badge.svg)](https://github.com/rybosome/mic/actions/workflows/ci.yml)

`mic` - short for "micro-evals".

Typed evaluations for LLMs, agents, and other nondeterministic systems. Define
your cases, scoring functions, and application call; `mic` runs repeated trials
and streams results with optional local evidence.

## Overview

The purpose of `mic` is to make it simple to author and run minimal, code-first declarations of
evaluation for non-deterministic systems.

Defining an evaluation consists of 3 things:

 1) **which cases to test**
 2) **how to determine answer quality**
 3) **how to call the code under evaluation**

`mic` evals are files which combine all 3, forming an execution contract for the accompanying CLI.

A typical `mic` file has the following structure:

```python
# my_eval.py

import mic

@mic.dataset(input=..., expected=...)
def my_dataset():
    """Define the cases we are testing."""
    pass

@mic.scorer()
def my_scorer(...):
    """Determine how a single input+expected+output row of our dataset performed."""
    pass

@mic.eval(dataset=my_dataset, scorers=[my_scorer])
def my_task(...):
    """Execute the task against the given dataset and scorer, row-by-row."""
    pass
```

The `mic` CLI command `mic run my_eval:my_task` will stream the dataset through the task/scoring
pipeline, optionally preparing local reports and evidence as requested.

### Agent skill

`mic` is explicitly designed for ease of agent authorship and execution.

Give your agent [the mic skill](skills/mic/SKILL.md) for installation, evaluation
design, CLI controls, and result interpretation.

## Install

Requires **Python 3.12+**. The core has no third-party runtime dependencies.

### uv

Add `mic` to your project with [uv](https://docs.astral.sh/uv/):

```console
uv add mic-evals
```

The commands below use the activated environment. To activate:
 - Mac/Linux: `source .venv/bin/activate`
 - Windows PowerShell: `.venv\Scripts\Activate.ps1`

Without activation, use `uv run mic` in place of `mic`.

### pip

Run `python -m pip install mic-evals` in your activated project environment.

## Quickstart: evaluate a support-ticket classifier

This example uses Jev to classify tickets as bugs, feature requests, or questions.

```console
uv add mic-evals typesafe-sdk
```

Set `TYPESAFE_API_KEY` in your environment, and save the following as [ticket_eval.py](examples/ticket_eval.py).

```python
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
```

### Run and inspect

```console
mic run ticket_eval:classify
```

```text
Run          aab09342b2884fd4a5b54b41c8b58e3c
Status       completed
Trials       3 completed / 3 admitted
Failures     0 task, 0 scoring
classify.accuracy: mean=1.0 count=3
Source       tickets: 3 accepted, 0 rejected
```

In this example run, all three trials completed and each prediction matched its expected label.

Run again with saved evidence, then generate a report:

```console
mic run ticket_eval:classify --output .mic/tickets
mic report .mic/tickets --open
```

```text
.mic/tickets/
├── run.json      # Final summary, requirements, and run outcomes
├── events.jsonl  # Individual cases, predictions, scores, and errors
└── report.html   # Generated by mic report
```

Without `--output`, `mic` saves no files; choose an empty or new directory for each recorded run.

## Datasets: keep the evaluation, change the source

### Optional Pydantic models

If your application already uses Pydantic, install `mic`'s optional support:

```console
uv add 'mic-evals[pydantic]'
```

Replace the quickstart's `Ticket` and `Classification` definitions with these
models, keeping its `Label` alias and other imports:

<!-- snippet: pydantic-models -->
```python
from pydantic import BaseModel


class Ticket(BaseModel):
    subject: str
    body: str


class Classification(BaseModel):
    label: Label
```

See [structured schemas](docs/schemas.md).

### Local JSONL

Save these records as `tickets.jsonl` beside `ticket_eval.py`:

<!-- snippet: tickets-jsonl -->
```jsonl
{"input":{"subject":"PDF upload","body":"The app closes whenever I upload a PDF."},"expected":{"label":"bug"}}
{"input":{"subject":"Invoice export","body":"Can you add an option to export invoices?"},"expected":{"label":"feature"}}
{"input":{"subject":"Past invoice","body":"Where can I download last month's invoice?"},"expected":{"label":"question"}}
```

Replace the quickstart's `tickets` factory and add these imports:

<!-- snippet: file-dataset -->
```python
from pathlib import Path

from mic.providers.files import JSONLFileHandle


@mic.dataset(input=Ticket, expected=Classification)
def tickets() -> JSONLFileHandle:
    return JSONLFileHandle(Path(__file__).with_name("tickets.jsonl"))
```

See
[local file contracts and custom row mapping](docs/providers.md#local-files).

### BigQuery

```console
uv add 'mic-evals[bigquery]'
```

Configure Application Default Credentials. Replace the dataset factory with this
one, using your billing project, location, and table (columns: `id`, `subject`,
`body`, `label`):

<!-- snippet: bigquery-dataset -->
```python
from mic.providers.bigquery import BigQueryHandle


@mic.dataset(input=Ticket, expected=Classification)
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
uv add 'mic-evals[braintrust]'
```

Set `BRAINTRUST_API_KEY`. Use an existing dataset with the same `input` and
`expected` objects as the JSONL above, pinned to an explicit version:

<!-- snippet: braintrust-dataset -->
```python
from mic.providers.braintrust import BraintrustHandle


@mic.dataset(input=Ticket, expected=Classification)
def tickets() -> BraintrustHandle:
    return BraintrustHandle(
        dataset_id="your-dataset-id",
        xact_id="your-pinned-version",
    )
```

Factories describe sources; reading a cloud dataset contacts that service.
See [provider setup and limits](docs/providers.md).

### Custom sources

Here is a simple reader for YAML documents delimited by `---`. Install `pyyaml`:

```console
uv add pyyaml
```

Save these tickets as `tickets.yaml`:

<!-- snippet: tickets-yaml -->
```yaml
input:
  subject: PDF upload
  body: The app closes whenever I upload a PDF.
expected:
  label: bug
---
input:
  subject: Invoice export
  body: Can you add an option to export invoices?
expected:
  label: feature
---
input:
  subject: Past invoice
  body: Where can I download last month's invoice?
expected:
  label: question
```

Start with a factory that yields records.

<!-- snippet: yaml-dataset -->
```python
from collections.abc import Iterator
from pathlib import Path

import mic
import yaml


@mic.dataset(input=Ticket, expected=Classification)
def tickets() -> Iterator[object]:
    path = Path(__file__).with_name("tickets.yaml")
    with path.open("rb") as stream:
        try:
            yield from yaml.safe_load_all(stream)
        except yaml.YAMLError:
            raise mic.DatasetError("Invalid YAML document stream") from None
```

For greater control over runtime behavior, accept `ctx` to pass the remaining
row selection and read time into the reader:

<!-- snippet: yaml-context-dataset -->
```python
from collections.abc import Iterator
from itertools import islice
from urllib.request import urlopen

import mic
import yaml


@mic.dataset(input=Ticket, expected=Classification)
def tickets(ctx: mic.ReadContext) -> Iterator[object]:
    url = "https://example.com/tickets.yaml"
    ctx.set_provenance(provider="yaml", url=url)
    timeout = ctx.remaining_seconds
    if timeout is not None and timeout <= 0:
        raise TimeoutError
    with urlopen(url, timeout=timeout) as stream:
        try:
            yield from islice(yaml.safe_load_all(stream), ctx.remaining_rows)
        except yaml.YAMLError:
            raise mic.DatasetError("Invalid YAML document stream") from None
```

To package either style as a reusable, configurable source, implement
`mic.DatasetSource`:

<!-- snippet: yaml-source-dataset -->
```python
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import islice
from urllib.request import urlopen

import mic
import yaml


@dataclass(frozen=True)
class YamlDocuments(mic.DatasetSource):
    url: str

    def read(self, ctx: mic.ReadContext) -> Iterator[object]:
        ctx.set_provenance(provider="yaml", url=self.url)
        timeout = ctx.remaining_seconds
        if timeout is not None and timeout <= 0:
            raise TimeoutError
        with urlopen(self.url, timeout=timeout) as stream:
            try:
                yield from islice(yaml.safe_load_all(stream), ctx.remaining_rows)
            except yaml.YAMLError:
                raise mic.DatasetError("Invalid YAML document stream") from None


@mic.dataset(input=Ticket, expected=Classification)
def tickets() -> YamlDocuments:
    return YamlDocuments("https://example.com/tickets.yaml")
```

See [custom source contracts](docs/providers.md#custom-sources) for context,
resource ownership, and timeout semantics.

## Scoring: multiple metrics and supporting evidence

Each scorer produces **one named metric**;
attach multiple scorers to an evaluation. Return a number, `mic.Score` with JSON
metadata, or `None` when a metric does not apply.

Replace the quickstart's scorer block with:

<!-- snippet: extended-scoring -->
```python
@mic.scorer()
def accuracy(ctx: mic.ScoreContext[Ticket, Classification]) -> mic.Score:
    expected = ctx.require_expected().label
    return mic.Score(
        value=float(ctx.output.label == expected),
        metadata={"body_length": len(ctx.input.body)},
    )


@mic.scorer()
def bug_recall(ctx: mic.ScoreContext[Ticket, Classification]) -> float | None:
    if ctx.require_expected().label != "bug":
        return None
    return float(ctx.output.label == "bug")
```

Then change the task's decorator to:

<!-- snippet: multiple-scorers -->
```python
@mic.eval(dataset=tickets, scorers=[accuracy, bug_recall])
```

See [scoring contracts](docs/api.md).

## Tasks: sync, async, and execution context

`mic` accepts async tasks and scorers; they can be mixed in the same evaluation.

### Use an async client

Replace the quickstart's task with this async equivalent using TypeSafe's
[`AsyncTypeSafeClient`](https://docs.typesafe.ai/sdk/python).
The dataset, scorer, and CLI commands stay the same.

<!-- snippet: async-task -->
```python
@mic.eval(dataset=tickets, scorers=[accuracy])
async def classify(ticket: Ticket) -> Classification:
    from typesafe_sdk import AsyncTypeSafeClient, Choice, RetryPolicy

    async with AsyncTypeSafeClient(timeout=30, retry=RetryPolicy(max_retries=0)) as client:
        response = await client.system_one(
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
```

### Request context when you need it

For case/trial logging, replace the async task's signature with the following
and add the logging call before its existing body. Keep its decorator unchanged:

<!-- snippet: task-context -->
```python
async def classify(ctx: mic.TaskContext, ticket: Ticket) -> Classification:
    import logging

    logging.getLogger(__name__).info("case=%s trial=%s", ctx.case_id, ctx.trial)
```

See [context types](docs/api.md#callback-signatures-and-context-types).

## Use the CLI

Run a specific evaluation, control repeated execution, and enforce quality
thresholds without writing a runner.

### Discover and validate

```console
# List datasets, scorers, and evaluations without loading data.
mic list ticket_eval

# Inspect three validated cases without calling the task.
mic inspect ticket_eval:tickets --limit 3

# Validate the full dataset and execution configuration without calling the task.
mic preflight ticket_eval:classify
```

At the inspection limit, `exhausted: false` means `mic` stopped without checking for
another record, not that more records necessarily exist.
Inspection and preflight read datasets, including remote sources.

### Control the run

```console
# Five trials per case, at most two concurrent tasks, and a cooperative 30-second
# per-trial timeout.
mic run ticket_eval:classify \
  --trials 5 \
  --concurrency 2 \
  --timeout 30 \
  --output .mic/tickets-v2
```

See [execution controls and limits](docs/cli.md#execution-controls-and-limits)
for row selection, execution caps, and timeout behavior.

### Turn scores into pass/fail requirements

With both scorers from the preceding section attached:

<!-- snippet: cli-gates -->
```console
mic run ticket_eval:classify \
  --trials 5 \
  --require 'tasks["classify"].scores["accuracy"].mean>=0.9' \
  --require 'tasks["classify"].scores["bug_recall"].mean>=0.95' \
  --require 'tasks["classify"].scores["bug_recall"].count>=5' \
  --require 'trials.task_failed==0' \
  --require 'trials.task_ms.max<=2000'
```

See [quality gates](docs/cli.md#quality-gates) for full semantics.

### Inspect or automate the results

```console
# Render saved evidence without rerunning the task.
mic report .mic/tickets-v2 --open

# Execute a new run, save evidence, and print the final manifest as JSON.
mic run ticket_eval:classify --output .mic/tickets-json --json

# Read the saved manifest without calling the model again.
python -m json.tool .mic/tickets-json/run.json
```

For all commands, see the
[CLI reference](docs/cli.md), or explore the built-in help:

```console
mic --help
mic run --help
```

## Run from Python

In a separate script, import the evaluation and call `mic.run()`:

<!-- snippet: programmatic-run -->
```python
import mic
from ticket_eval import classify

result = mic.run(
    classify,
    trials=5,
    concurrency=2,
    timeout=30,
    require=['tasks["classify"].scores["accuracy"].mean>=0.9'],
)
print(result.summary.tasks["classify"].scores)
raise SystemExit(result.exit_code)
```

In a notebook or async application with an active event loop, use `mic.arun()`:

<!-- snippet: programmatic-arun -->
```python
import mic
from ticket_eval import classify

result = await mic.arun(
    classify,
    trials=5,
    concurrency=2,
    require=['tasks["classify"].scores["accuracy"].mean>=0.9'],
)
result.summary.tasks["classify"].scores
```

Either runner supports sync and async tasks.
See the [Python execution API](docs/api.md#multiple-evaluations) for suite semantics.

## Notes and documentation

- **Early release:** the API may change before a stable release.
- **Sensitive evidence:** recorded inputs, outputs, metadata, and scores may contain private data.
  Review artifacts before sharing them.

[Artifacts](docs/artifacts.md) · [Optional Braintrust reporting](docs/reporting.md) ·
[Verification and offline demo](docs/verification.md) ·
[Contributing](CONTRIBUTING.md) · [Releasing](docs/releasing.md) · [MIT license](LICENSE)
