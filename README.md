# 🎤 mic

[![CI](https://github.com/rybosome/mic/actions/workflows/ci.yml/badge.svg)](https://github.com/rybosome/mic/actions/workflows/ci.yml)

Typed evaluations for LLMs, agents, and other nondeterministic systems. Define
your cases, scoring functions, and application call; Mic runs repeated trials
and streams results with optional local evidence.

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


@mic.dataset(input=Ticket, expected=Classification)
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
@mic.scorer()
def accuracy(ctx: mic.ScoreContext[Ticket, Classification]) -> float:
    return float(ctx.output.label == ctx.require_expected().label)
```

### 3. Define the task

Call your application and return its result. Here, the
[OpenAI structured-output parser](https://developers.openai.com/api/docs/guides/structured-outputs)
uses the same `Classification` model as Mic.

<!-- snippet: quickstart-task -->
```python
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
```

### Run and inspect

```console
mic run ticket_eval:classify --output .mic/tickets
mic report .mic/tickets --open
```

See each ticket, expected answer, prediction, and score in a standalone HTML report;
JSON/JSONL evidence is saved alongside it.

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
Both work with Mic's dataset and output schemas. See [structured schemas](docs/schemas.md).

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


@mic.dataset(input=Ticket, expected=Classification)
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
uv pip install 'mic-evals[braintrust]'
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

Start with a factory that yields records. It doesn't need to accept `ctx`.
This example reads one YAML document per case, separated by `---`:

```console
uv add pyyaml
```

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

For a remote YAML file, accept `ctx` to pass the remaining row selection and
read time into the reader:

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
`mic.DatasetSource`. Here the URL becomes configuration, and the reading logic
moves into `read`:

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

Mic accepts async tasks and scorers; they can be mixed in the same evaluation.

### Use an async client

Replace the quickstart's task with this async equivalent. OpenAI supports
[`AsyncOpenAI` with awaited structured-output parsing](https://developers.openai.com/cookbook/examples/partners/eval_driven_system_design/receipt_inspection).
The dataset, scorer, and CLI commands stay the same.

<!-- snippet: async-task -->
```python
@mic.eval(dataset=tickets, scorers=[accuracy])
async def classify(ticket: Ticket) -> Classification:
    from openai import AsyncOpenAI

    async with AsyncOpenAI(timeout=30, max_retries=0) as client:
        response = await client.responses.parse(
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

# Print the final run manifest as JSON for automation.
mic run ticket_eval:classify --json
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
