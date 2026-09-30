# mic for Python

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
python -m pip install mic-evals openai
```

Mic itself has no third-party runtime dependencies. `openai` is only for the
classifier below; replace that function with your own model or application call.
The example uses the [OpenAI Python SDK](https://developers.openai.com/api/docs/libraries)
and [GPT-4.1 mini](https://developers.openai.com/api/docs/models/gpt-4.1-mini).
Set `OPENAI_API_KEY` in your environment using your usual secret-management method.
Running it sends the example messages to OpenAI and incurs normal API charges.

Save this complete example as `ticket_eval.py`:

```python
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
```

The dataset supplies the cases, the task calls the classifier, and the scorer gives
each answer a `1` for a matching label or `0` otherwise. An unexpected label or extra
explanation scores zero too. These three illustrative cases make the example easy
to run; a useful evaluation needs a larger, representative set of labeled messages.

Run it from the directory containing `ticket_eval.py`, then open the report:

```console
mic run ticket_eval:classify --output .mic/tickets-first
mic report .mic/tickets-first --open
```

That's three classifier calls. The report shows each message, its expected label,
the actual response, and its score, alongside aggregate accuracy and execution
failures. Start with the cases that scored zero: was the answer wrong, was the
expected label ambiguous, or did the response include unwanted prose?

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
{"id":"upload","input":"The app closes whenever I upload a PDF.","expected":"bug"}
{"id":"export","input":"Can you add an option to export invoices?","expected":"feature"}
{"id":"invoice","input":"Where can I download last month's invoice?","expected":"question"}
```

Replace the `tickets` definition with this, adding the two imports:

```python
from pathlib import Path

from mic.providers.files import FileHandle


@mic.dataset(name="tickets", schema=mic.case_schema(input=str, expected=str))
def tickets() -> FileHandle:
    return FileHandle(Path(__file__).with_name("tickets.jsonl"))
```

The task, scorer, and run command stay the same. Add cases from real failures as
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
