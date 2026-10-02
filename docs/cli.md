# CLI reference

Mic's CLI discovers Python definitions, validates datasets, runs evaluations, and
renders saved evidence. It uses the same execution engine as the Python API.
Start with the [README walkthrough](../README.md#use-the-cli).

## Select a definition

Run commands from a directory where your evaluation module is importable:

```console
mic list ticket_eval
mic list ticket_eval another_eval --json
mic run ticket_eval:classify
```

`module:symbol` selects an exact public Python symbol, not its decorator's `name`.
Package paths such as `evals.tickets:classify` work too. There is no glob selection
or automatic suite discovery. `list` accepts one or more explicit module names
and lists datasets, evaluations, and scorers with their selectors.

Only import trusted modules: importing Python executes their top-level code.
Mic's discovery does not invoke dataset factories or initialize provider clients.

## Commands

| Command | Accepts | Effect |
| --- | --- | --- |
| `list` | One or more modules | List definitions; no dataset reads or task calls. Optional `--json`. |
| `inspect` | Dataset or evaluation selector | Read, validate, and print dataset information as JSON. `--limit N` explicitly selects a prefix. |
| `preflight` | Evaluation selector | Read the full dataset and validate execution configuration without calling tasks or scorers. Prints JSON. |
| `run` | Evaluation selector | Execute trials/scorers and save local evidence. Optional `--json` prints the final manifest. |
| `report` | Run directory or `run.json` | Render existing evidence as standalone HTML; no evaluation rerun. |

Inspection, preflight, and runs read cloud datasets when selected. Each invocation
loads its own snapshot; preflight does not cache data for a later run.

## Execution controls and limits

```console
mic preflight ticket_eval:classify --trials 5 --max-executions 1000
mic run ticket_eval:classify --trials 5 --concurrency 2 --timeout 30
```

`--trials` and `--concurrency` override the evaluation's decorator settings
(defaults: 1 trial and concurrency 10). Preflight accepts both overrides, but
executes no trials. Trials apply to every dataset case; `run` has no case-filter
or prefix-selection flag.

| Option | Commands | Default / meaning |
| --- | --- | --- |
| `--max-executions` | `preflight`, `run` | 50,000; cap on cases × trials. |
| `--timeout` | `run` | Unset; cooperative per-trial deadline in seconds, covering task and scorers. |
| `--max-rows` | `inspect`, `preflight`, `run` | 10,000 rows. |
| `--max-bytes` | `inspect`, `preflight`, `run` | 67,108,864 bytes. |
| `--max-record-bytes` | `inspect`, `preflight`, `run` | 1,048,576 bytes per record. |
| `--dataset-timeout` | `inspect`, `preflight`, `run` | 60 seconds; dataset materialization deadline. |

Safety caps fail visibly instead of silently truncating the dataset. Use
`inspect --limit N` when you intentionally want to inspect only a prefix.
See [provider limits](providers.md#shared-limits-and-identity) for raw and
normalized size accounting.

Timeouts are cooperative, not hard process-kill deadlines. Already-running
synchronous calls cannot be forcibly stopped; Mic waits for in-flight work
before resource cleanup. Set SDK/network timeouts too. See
[execution and cancellation](api.md#execution-and-errors).

## Quality gates

With the README's `accuracy` and `bug_recall` scorers attached:

```console
mic run ticket_eval:classify \
  --trials 5 \
  --require 'accuracy>=0.9' \
  --require 'bug_recall>=0.95'
```

- Repeat `--require` to add conditions; all must pass. Quote expressions so the
  shell does not interpret `<` or `>` as redirection.
- Expressions use a declared scorer's name, one of `>=`, `>`, `<=`, `<`, `==`,
  and a finite numeric threshold. For a lower-is-better metric, use `<=` or `<`.
- Each comparison uses the metric's mean over available numeric scores across
  cases and trials. `None` is excluded, not scored as zero. A metric with no
  numeric scores fails its gate.
- Invalid expressions and undeclared metrics are configuration errors. There are
  no boolean expressions, per-case gates, percentile gates, or baseline comparisons.
- Gate failure does not convert a completed trial into an execution failure.
  Evidence records each gate's expression, actual mean, and pass/fail result.
  Execution failures still make the run fail even if its score gates pass.

Thresholds above are illustrative, not recommendations for your application.
Choose representative cases and requirements appropriate to the task. In CI,
the command's exit status is sufficient to fail a step; no wrapper is required.

## Outputs and automation

```console
mic run ticket_eval:classify --output .mic/tickets-v2 --json
mic report .mic/tickets-v2 --open
mic report .mic/tickets-v2/run.json --output ticket-report.html
```

An explicit run output directory must be empty (or not yet exist). Omit
`--output` for a unique directory under `.mic`. Runs produce local JSON/JSONL
evidence and an HTML report; `report` can regenerate HTML without model calls.
`report --output` chooses the HTML destination, not a new run directory.

`run --json` prints the complete final manifest instead of the human summary.
Failed quality gates still produce a manifest and saved evidence. Check exit
status as well as JSON; setup failures can exit before a manifest exists.
Application code that prints to stdout can interfere with machine consumption,
so keep such logging on stderr. See [artifact formats](artifacts.md).

### Optional Braintrust export

```console
mic run ticket_eval:classify \
  --braintrust-project your-project \
  --braintrust-experiment ticket-classifier-v2
```

This opts into a remote write after local evidence is finalized. It requires the
Braintrust extra, credentials, and your intended project. Optional
`--braintrust-app-url` and `--braintrust-org` configure the destination; these
options and `--braintrust-experiment` require `--braintrust-project`.
Preflight accepts the same reporter options to validate configuration without
exporting a run. See [reporting setup and sensitive evidence](reporting.md).

## Exit codes and help

| Code | Meaning |
| --- | --- |
| `0` | Command succeeded; for a run, execution and requirements succeeded. |
| `1` | Execution, quality gate, reporting, or other operational failure. |
| `2` | Configuration, dataset validation, or argument parsing error. |
| `130` | Interrupted. |

```console
mic --help
mic run --help
mic inspect --help
```

Inputs, outputs, and errors are not automatically redacted. Review saved evidence
and destination settings before sharing or exporting a run.
