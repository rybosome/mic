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
Package paths such as `evals.tickets:classify` work too. For a suite, use
`mic run 'ticket_eval:*'`: all public evaluations in that module, in sorted symbol
order, with aliases deduplicated. This is the only wildcard form; it does not scan
packages or accept partial-symbol globs. Duplicate evaluation names are rejected
before source reads. `list` accepts explicit module names and lists definitions.

Only import trusted modules: importing Python executes their top-level code.
Mic's discovery does not invoke dataset factories or initialize provider clients.

## Commands

| Command | Accepts | Effect |
| --- | --- | --- |
| `list` | One or more modules | List definitions and each evaluation's declared sink names; no dataset reads, task calls, or sink opens. Optional `--json`. |
| `inspect` | Dataset or evaluation selector | Read, validate, and print dataset information as JSON. `--limit N` explicitly selects a prefix. |
| `preflight` | Evaluation selector | Read the full dataset and validate execution configuration without calling tasks or scorers. Prints JSON. |
| `run` | Evaluation selector or `module:*` | Stream trials/scorers with [progress](#progress-output) on stderr; `--output` opts into evidence. Optional `--json` prints the final manifest; `-q`/`-v` adjust progress. |
| `report` | Run directory or `run.json` | Render existing evidence as standalone HTML; no evaluation rerun. |

Inspection, preflight, and runs read cloud datasets when selected. Each invocation
reads a fresh stream; preflight does not cache data for a later run.

## Execution controls and limits

```console
mic preflight ticket_eval:classify --trials 5 --max-executions 1000
mic run ticket_eval:classify --trials 5 --concurrency 2 --timeout 30
```

`--trials` and `--concurrency` override the evaluation's decorator settings
(defaults: 1 trial and concurrency 10). Preflight accepts both overrides, but
executes no trials. For suites, concurrency is global (default: largest selected
decorator value), not multiplied per evaluation. Trials override all selected tasks;
otherwise each uses its own default. `--max-executions` is global, source limits
apply per distinct Dataset object, and identical Dataset objects share one read.
Trials apply to every accepted dataset case. `--limit` selects raw source
records before validation; there is no case-filter flag.

| Option | Commands | Default / meaning |
| --- | --- | --- |
| `--max-executions` | `preflight`, `run` | 50,000; cap on cases × trials. |
| `--timeout` | `run` | Unset; cooperative per-trial deadline in seconds, covering task and scorers. |
| `--limit` | `inspect`, `preflight`, `run` | Unset for run/preflight; 20 for inspection. Selects raw records, including rejected records. |
| `--dataset-timeout` | `inspect`, `preflight`, `run` | Unset; cumulative active factory/read/mapping time, excluding downstream waiting. |

Selection ends successfully without reading beyond the requested count.
Natural exhaustion is unconfirmed when the selection is reached.
`run --on-invalid skip` skips recognized row mapping/schema errors; default `abort`
stops admission. Transport/iterator errors, timeouts, and the execution cap fail. Previously admitted
trials finish; an empty or all-rejected evaluation fails.
There is no dataset or per-record byte cap. See
[provider limits](providers.md#shared-limits-and-identity) for memory implications.
The separate `report --max-bytes` cap still bounds evidence loaded for HTML rendering.

Timeouts are cooperative, not hard process-kill deadlines. Already-running
synchronous calls cannot be forcibly stopped; Mic waits for in-flight work
before resource cleanup. Set SDK/network timeouts too. See
[execution and cancellation](api.md#execution-and-errors).

## Progress output

`mic run` reports progress on stderr while it streams; stdout carries only the
final summary (or the `--json` manifest), so piping stdout is unaffected.

```text
mic run 126050df · necessity · 3 trials/case · concurrency 8 · timeout 30s
  ✓✓✓    5.8s  row 2
  ✓✗✓   30.4s  row 6 (refund-edge) · trial 2 task_failed (TimeoutError)
  ✓✓!    7.1s  row 9 · trial 3 scoring_failed in recall (KeyError)
⠼ 14 cases · 38 trials done · 4/8 running · 1 failed · 0.7/s · 0:52
```

- The header names the run, its evaluations, and execution options.
- Each case prints one line when its last trial finishes, in completion order (row
  numbers can appear out of order). Marks follow trial order: `✓` completed,
  `✗` task failed, `!` scoring failed, `·` cancelled or never run. A rejected record
  (with `--on-invalid skip`) prints `rejected` with its exception type. The time is the
  slowest trial's total. Suites name each evaluation's marks, and runs that read more
  than one source name the source.
- Failures show the phase, scorer, and exception type only, like recorded evidence;
  exception messages are never printed.
- In a terminal, a status line is redrawn in place: cases (or records against
  `--limit`), finished, running, and failed trials, throughput, elapsed time, and a
  trial that has run unusually long. It also says when Mic is reading a source,
  finishing sinks, or waiting for running trials after Ctrl-C. Already-running
  synchronous task code cannot be interrupted, so cancellation waits for it to return.
- When stderr is not a terminal (CI logs, redirects) or `TERM=dumb`, there is no
  redrawn line or color: Mic prints the header, failed and rejected cases, and a status
  line every 30 seconds and when a run starts cancelling or finishing.
- `-v`/`--verbose` prints every case, including when stderr is not a terminal, with each
  case's score means, for example `exact=0.75 recall=–` (`–`: no score recorded).
- `-q`/`--quiet` prints no progress at all.

Progress never changes a run's result, exit code, or evidence. Set `NO_COLOR` to
disable color. Glyphs fall back to ASCII when stderr's encoding cannot represent them.
While the status line is shown, Mic briefly wraps `sys.stdout` and `sys.stderr` so that
application `print()` output appears whole above it; output without a trailing newline
is held until the line completes. Logging handlers created before the run keep the
original stream and may share a line with the status line until its next redraw.

## Quality gates

With the README's `accuracy` and `bug_recall` scorers attached:

```console
mic run ticket_eval:classify \
  --trials 5 \
  --require 'tasks["classify"].scores["accuracy"].mean>=0.9' \
  --require 'tasks["classify"].scores["bug_recall"].min>=0.95'
```

- Repeat `--require` to add conditions; all must pass. Quote expressions so the
  shell does not interpret `<` or `>` as redirection.
- Expressions use an explicit summary path, one of `>=`, `>`, `<=`, `<`, `==`, `!=`,
  and a finite numeric threshold. For a lower-is-better metric, use `<=` or `<`.
- Choose a score's `.count`, `.mean`, `.min`, or `.max`, or use trial counters
  and timing statistics, e.g. `trials.task_failed == 0` or
  `tasks["classify"].trials.task_ms.max <= 2000`. `None` scores are excluded,
  not scored as zero. A missing observation fails even a `!=` comparison.
- Invalid expressions and undeclared metrics are configuration errors. There are
  no boolean expressions, per-case gates, percentile gates, or baseline comparisons.
- Gate failure does not convert a completed trial into an execution failure.
  Evidence records each gate's expression, actual value, and pass/fail result.
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
`--output` to write no files. With it, runs record `events.jsonl` and `run.json`;
`report` explicitly generates HTML without model calls.
`report --output` chooses the HTML destination, not a new run directory.

Without `--json`, `run` prints a human summary on stdout: status, elapsed wall-clock
time, trial and failure counts, one aligned `Score` line per metric, requirement
results, and source counts. Score means are rounded to three decimal places (very small
or very large means use an exponent; `n/a` means no score was recorded). Requirement
lines show the exact actual value, so a rounded mean never appears to contradict
PASS/FAIL. `run.json` and `--json` keep exact values.

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

This opts into remote writes as trials complete. Local recording is independent
and opt-in; use `--output` as well when you want a local copy. It requires the
Braintrust extra, credentials, and your intended project. Optional
`--braintrust-app-url` and `--braintrust-org` configure the destination; these
options and `--braintrust-experiment` require `--braintrust-project`.
See [reporting setup and sensitive evidence](reporting.md).

### Declared sinks

An evaluation declared with `@mic.eval(..., sinks=[...])` sends results to those
sinks on every `mic run`, with no flags. `mic list` shows their names
(`sinks=` in text, a `sinks` array per evaluation with `--json`) without opening
them. Explicit flags such as `--braintrust-project` add to declared sinks; the
recorder is first, explicit sinks next, then declared sinks. In a `module:*`
suite each declared sink receives only the evaluations that declared it.

```console
mic run ticket_eval:classify --no-sinks
```

`--no-sinks` ignores every declared sink for that run, for example to
run offline without publishing. It does not disable explicitly requested sinks:
`--output` and `--braintrust-project` still apply, and Mic has no default sinks. See
[declared sinks](reporting.md#declared-sinks).

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

Inputs, outputs, metadata, and scores are not automatically redacted.
Exception messages in persisted failure records are generic; original SDK bodies
and tracebacks are not captured. Review saved evidence
and destination settings before sharing or exporting a run.
