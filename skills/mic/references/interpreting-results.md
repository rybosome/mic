# Interpreting results and choosing the next step

Start with the process exit status and finalized manifest, then examine individual
trial evidence where needed. A high mean does not establish complete coverage,
successful execution, or successful recording.

## What is saved

```text
chosen-output/
├── run.json      # Final manifest: summary, sources, requirements, sinks, configuration
├── events.jsonl  # Cases, rejected records, source outcomes, and individual trials
└── report.html   # Optional; generated afterward by mic report
```

Read the current `schema_version`: these references cover `mic-run-v4` and
`mic-event-v1`. An initial `status: running` marker is not a completed result.
Use documentation matching the artifact version; do not reinterpret an unknown
version as the current schema. No files are saved unless recording was enabled.

## Read a manifest

| Field | Question to answer |
| --- | --- |
| `status`, `exit_code` | Did the run finalize successfully, fail, or get cancelled? |
| `sources` | How many records were seen/accepted/rejected; did the source exhaust or fail? |
| `summary.trials` | How much admitted work completed or failed? |
| `summary.tasks[name].scores[metric]` | What numeric observations support this statistic? |
| `requirements` | Which expressions passed, and what actual value/reason was recorded? |
| `failures` | Which bounded run-level failures were reported? |
| `sinks` | Was local recording or remote delivery successful, including finalization? |
| `info.tasks` | Which schemas, resolved options, read limits, and provenance apply? |

`summary.trials.planned` counts admitted trials, not all potential work in unread
rows. `completed`, `task_failed`, `scoring_failed`, and `cancelled` partition those
trials after cleanup. `scoring_skipped` counts trials with at least one explicit
null score and can overlap a terminal outcome; do not add it to that partition.

Statistics contain `count`, `mean`, `min`, `max`; empty observations have zero count
and null statistics. A score of zero is a numeric observation. A score of null is
not applicable and excluded. Missing scores from work that never ran are not null
observations. Successful scores can survive another scorer's failure.

For example: accuracy mean 1.0 with count 2 and one task failure means both observed
answers were correct, but the evaluation failed and the third answer is unknown.
Likewise, three completed trials with accuracy 0.33 and no requirements can exit 0:
execution succeeded, not the quality objective. Add an explicit quality requirement
when that objective should affect the process exit code.

## Source scope and comparable runs

At `--limit 3`, `exhausted: false` means no natural end was observed before
selection stopped. It does not prove that a fourth record exists. A source digest
then describes the selected normalized prefix, not the full dataset or source file.
Check `error` too: non-exhaustion is not always intentional selection.

The digest includes ordered normalized records and labels, excludes generated IDs
and physical provenance, and is not a hash of the original file bytes. Compare
scope, ordering, configuration, code, model, and metric definitions alongside it.
An SDK version or resolved remote model may be absent from provenance; retain a
lockfile and explicit model configuration for meaningful reproduction.

## Examine individual trials

Stream `events.jsonl` line by line for larger runs. Event types are
`case_accepted`, `record_rejected`, `source_finished`, and `trial_finished`.
Each trial event carries `task`, `case_id`, `source_id`, `row_index`, `trial`, and
its `result` (input, optional expected/output, scores, errors, status, latency).

- Events follow completion/delivery order, not dataset order. Join on source/case,
  task, and trial coordinates; don't zip lines to dataset rows. Trial numbers are
  one-based and row indices are zero-based. Case IDs are run-specific.
- A `source_finished` event can precede trial completions. Accepted cases may have
  no admitted trial when cancellation or execution caps intervene.
- Missing output differs from a valid explicit null. Missing expected differs
  from a supplied null reference. Do not coerce absent fields into null or zero.
- `failures` in the manifest is not a complete list of per-trial errors. Read trial
  events for the failed phase and exception type. Persisted messages intentionally
  omit raw exception bodies and tracebacks; do not guess the underlying cause.
- `task_ms`, `scoring_ms`, and `total_ms` are observed phase durations, not wall-clock
  run duration. They omit phases never started; total excludes queue/sink waiting.
  Revisit SDK and Mic deadlines using the [CLI timeout guidance](cli.md#choose-limits-deliberately).

Use `mic report DIR` for filtering/searching saved cases. It does not rerun tasks.
For a diagnosis requiring another run, choose a bounded scope and new output
directory via the [CLI action table](cli.md#from-observation-to-next-action).

## Partial evidence and claims

A failed trial does not erase successful trials. A source error can stop admission
while already-admitted work finishes. Failed gates preserve trial outcomes. Report
both useful observations and the failure; don't turn partial success into a clean pass.

Recording can fail independently of computation. Inspect every sink receipt;
evidence can be absent, stale, or truncated after interruption or disk failure.
A valid JSON line, a successful score, or an HTML report alone does not certify
complete recording/export. Don't silently discard a malformed final line or rerun
an expensive task merely to improve the presentation of an earlier failure.

Before sharing artifacts, review inputs, outputs, metadata, paths, and provider
provenance for sensitive data. Error sanitization is not comprehensive redaction.
Report distinct case coverage and trial counts, metric counts, rejected/skipped
work, requirements, and failures with the conclusion. Repeated trials on three
demo tickets remain a smoke test, not a production-quality benchmark.
