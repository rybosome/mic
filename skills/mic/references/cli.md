# CLI reference for agents

Use this reference when choosing commands or changing a run. Examples assume the
activated project environment and `ticket_eval.py` from the README; substitute the
user's actual selectors and declared metric names. Use `mic COMMAND --help` to
check options against the installed version.

## Discovery and selectors

```sh
mic list ticket_eval
mic list ticket_eval another_eval --json
mic inspect ticket_eval:tickets --limit 3
mic preflight ticket_eval:classify
```

- `list` takes one or more module names, prints kind/name/selector, and optionally
  emits JSON. It imports modules but does not call factories or tasks.
- `module:symbol` selects an exact public Python symbol, not the name passed to
  its decorator. Run from a directory where the module is importable.
- `inspect` accepts a dataset or evaluation; it prints validated rows and a
  source summary as JSON. Default selection: 20 raw records.
- `preflight` accepts one evaluation and validates its dataset/configuration,
  printing JSON with projected executions and zero executed tasks. It reads the
  full source unless limited. It does not execute scorers, test the live task,
  or cache data for a subsequent run.
- Only `run` supports `'module:*'`, selecting all public evaluations in that module
  in sorted symbol order, deduplicating aliases. Quote the selector. This is not
  a package search or partial-symbol glob. Duplicate evaluation names are errors.

Importing Python can run arbitrary top-level code. Inspection, preflight, and
execution read datasets, including remote sources when selected.

## Run controls

```sh
mic run ticket_eval:classify --limit 3 --trials 2 --concurrency 2 \
  --timeout 30 --dataset-timeout 15 --max-executions 6 \
  --output .mic/diagnostic --json
```

| Option | Commands | Default and effect |
| --- | --- | --- |
| `--trials N` | run, preflight | Overrides decorator trials (normally 1); executions per accepted case |
| `--concurrency N` | run, preflight | Overrides decorator concurrency (normally 10); global worker/queue bound |
| `--max-executions N` | run, preflight | 50,000; global admitted-trial cap, not a successful prefix selector |
| `--limit N` | inspect, preflight, run | 20 for inspect, unset otherwise; selects raw records before validation |
| `--dataset-timeout SECONDS` | inspect, preflight, run | Unset; cumulative active factory/read/mapping/validation time |
| `--timeout SECONDS` | run | Unset; cooperative per-trial deadline covering task/output validation/scorers |
| `--on-invalid abort\|skip` | run | `abort`; `skip` continues past recognized row mapping/schema errors |
| `--require EXPRESSION` | run | Repeatable quality requirements; all must pass |
| `--output DIR` | run | No files by default; opt into local evidence in a new or empty directory |
| `--json` | run | Print the complete final manifest instead of the human summary |

Counts must be positive integers; supplied timeouts must be finite and positive.
In suites, concurrency is global (default: largest selected decorator value).
Trials override every selected evaluation. Source limits apply per distinct
Dataset object; evaluations sharing that object share a read. Same names or SQL
do not establish source sharing. There is no per-case CLI filter.

### Choose limits deliberately

- Use `--limit` for a successful diagnostic prefix. It counts rejected records too
  and stops without probing beyond the selection. `exhausted=false` does not
  establish that more data exists.
- Use `--max-executions` to bound commitments. Exceeding the cap is a failure;
  don't use it as a substitute for row selection.
- Lower concurrency when diagnosing load/rate limits. Increasing trials increases
  application calls and potentially cost, not the number of distinct cases.
- Dataset time excludes downstream task/sink waiting. Trial time excludes queue
  and sink waiting. Neither is a hard process-kill deadline: already-running sync
  work cannot be forcibly stopped, and cleanup waits for it. Set SDK deadlines too.
- `--on-invalid skip` is a deliberate data-quality policy. Iterator/transport
  failures and timeouts remain fatal; an empty/all-rejected evaluation fails.
  Previously admitted work can still finish after source admission stops.

## Requirements and exit status

```sh
mic run ticket_eval:classify --trials 5 \
  --require 'tasks["classify"].scores["accuracy"].mean>=0.9' \
  --require 'tasks["classify"].scores["accuracy"].count>=15' \
  --require 'trials.task_failed==0' \
  --output .mic/gated --json
```

Requirement paths use evaluation and scorer names. Available score statistics
are `count`, `mean`, `min`, and `max`. Trial counters and timing statistics are
available globally (`trials.task_ms.max`) or per task
(`tasks["classify"].trials.task_ms.max`). Comparisons are `>=`, `>`, `<=`, `<`,
`==`, `!=` against finite numbers. Quote expressions for the shell.
There are no boolean expressions, percentile gates, or per-case gates.

An undeclared task/metric is a configuration error. An unobserved mean/min/max fails a
comparison, even `!=`; count remains numeric zero. Pair a mean requirement with
an appropriate count.
A failed gate does not turn a completed trial into an execution failure.
Passing gates do not override execution/source/sink failures. Without a quality
requirement, incorrect predictions can still produce exit code 0.

| Exit | Interpretation |
| --- | --- |
| 0 | Command succeeded; a run completed execution and passed its supplied requirements |
| 1 | Operational failure, including task, scorer, requirement, or sink/report failure |
| 2 | Argument/configuration/dataset failure, including a streaming source failure |
| 130 | Interrupted |

Check the process exit status as well as JSON. Setup failures can occur before a
manifest exists. For automation, keep application logging on stderr; application
prints to stdout can contaminate `--json`. Do not let a later report command's
exit status replace the evaluation's exit status in a script.

## Evidence and report controls

```sh
# Read existing results without running the model again.
python -m json.tool .mic/gated/run.json
mic report .mic/gated
mic report .mic/gated/run.json --output ticket-report.html --open
```

`run --output` writes `run.json` and `events.jsonl` directly into the chosen
directory; it does not create a run-ID subdirectory. `--json` alone does not save
files and always runs the evaluation. Use a fresh output directory for a rerun;
preserve earlier evidence rather than deleting it to reuse a name.

`report` consumes a run directory or `run.json`, with its sibling events file.
It generates `report.html` by default and prints its path. Its options are:

| Option | Default and effect |
| --- | --- |
| `--output FILE` | Choose the HTML filename, not a new evidence directory |
| `--max-cases N` | 10,000 trial records (despite the flag's name); fail if exceeded |
| `--max-bytes N` | 67,108,864 bytes (64 MiB) combined evidence input; fail if exceeded |
| `--open` | Request opening the generated HTML in the default browser |

Rendering makes no model calls. Increase caps only when the evidence size and
available memory justify it; cap failures do not mean the evaluation failed.
Browser opening depends on the host environment. A generated report is still
available at the printed path if a browser cannot open.

## Optional remote export

`run --braintrust-project PROJECT` opts into remote writes and requires the
Braintrust extra, configured credentials, and authorized destination.
`--braintrust-experiment NAME`, `--braintrust-app-url URL`, and
`--braintrust-org ORG` require `--braintrust-project`.
Local evidence remains a separate opt-in (`--output`); enabling export does not
save a local copy. Check sink receipts, including cleanup failures, before claiming
export succeeded. Read the [reporting contracts](https://github.com/rybosome/mic/blob/main/docs/reporting.md)
before using this integration.

## From observation to next action

| Observation | Next useful action |
| --- | --- |
| Missing/incorrect selector | `list MODULE`; check import root and public symbol |
| Unexpected inputs or labels | `inspect SELECTOR --limit N`; examine normalized rows |
| Need full data/configuration check | `preflight SELECTOR`; remember this rereads the source |
| Unexpected task failure | Inspect recorded trial errors and the task's environment; use a small `--limit` rerun if needed |
| Slow tasks | Inspect task versus scoring timings; review SDK deadlines and `--timeout` |
| Slow source | Review `--dataset-timeout`, provider request limits, and source behavior |
| Rate limits | Reduce `--concurrency`; inspect SDK retry policy before repeating calls |
| High mean, few observations | Inspect skipped/failed trials and class coverage; add a count requirement |
| Need variance on the same cases | Increase `--trials` within scope; keep data/scorers/configuration comparable |
| Need details from an old run | Read events or `report`; do not invoke `run --json` |

Use [interpreting results](interpreting-results.md) to distinguish these diagnoses
before changing execution parameters.
