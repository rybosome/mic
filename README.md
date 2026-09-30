# mic for Python

[![CI](https://github.com/rybosome/mic/actions/workflows/ci.yml/badge.svg)](https://github.com/rybosome/mic/actions/workflows/ci.yml)

Define typed micro-evaluations with decorators, read cases from Python data, local
files, BigQuery or Braintrust, and review every result locally. Dataset storage
and experiment reporting are independent. The core has **zero third-party runtime dependencies**. Standard-library dataclasses
are the primary schema API; Pydantic and cloud SDKs are optional.

mic is an early alpha. Its core execution and artifact contracts are well tested,
but its public API may still change before a stable release. The [schema guide](docs/schemas.md),
[provider guide](docs/providers.md), [reporting guide](docs/reporting.md), and
[verification guide](docs/verification.md) describe the current behavior.

**Try it now.** From this directory, use Python 3.12 and [uv](https://docs.astral.sh/uv/):

```console
uv sync --frozen
uv run mic list examples.triage
uv run mic inspect examples.triage:triage_data --limit 3
uv run mic preflight examples.triage:baseline
uv run mic run examples.triage:baseline --output .mic/baseline
uv run mic run examples.triage:fixed --output .mic/fixed
uv run mic report .mic/baseline --open
```

The baseline scores **2/3** and the fixed version **3/3**. Both execute successfully;
the baseline's first case shows `expected="bug"`, `output="question"`, and score 0.
Run directories must be empty so old evidence is never overwritten. Omit `--output`
to create a unique directory automatically.

The generated HTML report is self-contained and works without a server or network.

**Define complex cases with ordinary dataclasses.** Inputs, expected values, outputs,
and metadata can all be nested objects. No base class or mic field decorator is needed.

```python
from dataclasses import dataclass, field
import mic


@dataclass(frozen=True)
class Message:
    role: str
    text: str


@dataclass
class Ticket:
    messages: list[Message]
    context: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class Decision:
    label: str
    evidence: dict[str, list[int]] = field(default_factory=dict)


@mic.dataset(name="tickets", schema=mic.case_schema(input=Ticket, expected=Decision))
def tickets() -> list[mic.RawCase]:
    return [
        mic.RawCase(
            id="crash",
            input=Ticket([Message("user", "It crashes on startup")]),
            expected=Decision("bug", {"messages": [0]}),
        )
    ]


@mic.scorer(name="exact")
def exact(ctx: mic.ScoreContext[Ticket, Decision, Decision, mic.JsonObject]) -> float:
    return float(ctx.output == ctx.require_expected())


@mic.eval(name="classify", dataset=tickets, output=Decision, scorers=[exact])
def classify(ctx: mic.TaskContext[Decision, mic.JsonObject], ticket: Ticket) -> Decision:
    for index, message in enumerate(ticket.messages):
        if "crash" in message.text.lower():
            return Decision("bug", {"messages": [index]})
    return Decision("question")
```

Save this as `my_evals.py`, then run `uv run --no-dev mic run my_evals:classify`.
Provider JSON objects hydrate the same `Ticket`, nested `Message`, and `Decision`
instances. The runner validates task outputs and isolates nested mutable values
between trials and scorers. `run()` works in scripts; notebooks use `await arun()`.

The complete [structured example](examples/structured.py) includes native and JSONL
sources, typed metadata, nullable fields, and two scoring metrics:

```console
uv run --no-dev mic inspect examples.structured:tickets --limit 2
uv run --no-dev mic preflight examples.structured:classify
uv run --no-dev mic run examples.structured:classify --trials 3
uv run --no-dev mic run examples.structured:classify_file --trials 3
```

These commands do not install development or optional dependencies. For richer
constraints/aliases, explicitly use `pydantic_schema(MyModel)` from
`mic.integrations.pydantic` and `uv run --extra pydantic ...`. Existing BaseModel
classes are also adapted lazily when passed directly. Ordinary dataclasses always
use the native backend; Pydantic `Annotated` metadata needs the optional adapter.
See [schemas](docs/schemas.md) for the complete support table and boundary semantics.

**Use a provider.** Return a passive handle from the dataset factory:

```python
from pathlib import Path
from mic.providers.files import FileHandle
from mic.providers.bigquery import BigQueryHandle
from mic.providers.braintrust import BraintrustHandle

FileHandle(Path("fixtures/cases.jsonl"))
BigQueryHandle(
    billing_project="my-project",
    location="US",
    sql="SELECT id, input, expected FROM `my-project.evals.cases` ORDER BY id",
    maximum_bytes_billed=100_000_000,
)
BraintrustHandle(dataset_id="existing-id", xact_id="pinned-xact-id")
```

Install cloud extras with `uv sync --frozen --all-extras`. BigQuery uses Application
Default Credentials. Braintrust source reads use `BRAINTRUST_API_KEY` and an existing
dataset at an explicit version. The source adapters do not create datasets.
`mic estimate module:dataset` performs a BigQuery dry run without retrieving rows.
See [provider contracts and examples](docs/providers.md) for parameters, custom
providers, read limits, pagination, and opt-in live tests.

Every run writes local evidence. To additionally create a Braintrust experiment:

```console
uv run mic run examples.triage:fixed --braintrust-project my-eval-project
```

This is an explicit remote write. Numeric Braintrust scores must be in `[0,1]`;
local scores may be any finite numbers. Read [reporting](docs/reporting.md) for
upload failure handling, exact null representation, and the pinned SDK boundary.

**Interpret a run.** Execution outcomes and score quality are separate:

| Exit | Meaning |
| --- | --- |
| 0 | Execution succeeded and any explicit quality gates passed |
| 1 | A task/schema/scorer, artifact write, quality gate, or export failed |
| 2 | Invalid configuration or dataset preflight failure |
| 130 | Interrupted; partial local evidence retained where writable |

```console
uv run mic run examples.triage:baseline --require 'exact>=0.9'
uv run mic run examples.async_eval:uppercase --trials 3 --concurrency 2
```

The quoted gate compares the named numeric mean with its threshold. Null scores
do not become zero. Reports show numeric, unscored, and unavailable counts.
**Verify the package.**

```console
uv sync --frozen --all-extras
npm ci --ignore-scripts --prefix tests/reporting/js
uv run python scripts/verify.py
```

This runs tests, strict typing (including deliberately invalid authoring
examples), lint, formatting, branch coverage, report JavaScript syntax and DOM
interaction tests, and writes generated results under `.artifacts/verification/`.
Node 18+ and jsdom are development tools only; reports and the Python core need neither. Cloud tests require an explicit
opt-in and named fixture configuration; skipped live tests are not evidence of
remote correctness. The [verification guide](docs/verification.md) includes a short
human walkthrough and optional original-TypeScript reproduction.

The supported initial runtime is Python 3.12. Synchronous callbacks run in bounded
worker threads and must finish cooperatively; a timeout cannot kill Python threads.
The MVP materializes a finite validated snapshot before any task executes. Defaults
cap source rows, serialized bytes, record bytes, and row/trial executions. It does
not provide distributed execution, resume, unlimited streaming runs, automatic task
retries, dataset mutation, or a hosted UI.
