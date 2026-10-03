# Streaming artifact contract

Recording is opt-in: `mic run ... --output DIR` or `mic.run(..., output=DIR)`.
No files are written by default. Recording writes two files:

| File | Format | Purpose |
| --- | --- | --- |
| `run.json` | [mic-run-v3](artifact-schemas/run-v3.schema.json) | Compact final summary, source outcomes, requirements, sink receipts |
| `events.jsonl` | [mic-event-v1](artifact-schemas/event-v1.schema.json) | One independent, versioned event per line, in delivery order |

`mic report DIR` explicitly generates a standalone HTML view. It is not part of
run finalization. There is no dataset snapshot, implicit case collection, backward
reader, or migration. These alpha formats are forward-only.

[Shared definitions](artifact-schemas/common-v3.schema.json) describe statistics,
trials, sources, failures, and receipts. Schemas use Draft 2020-12; resolve their
references from the supplied files with date-time checking enabled. Tests validate
them offline. Runtime stays dependency-free; the report reader performs bounded
JSON/format checks, not full JSON Schema validation.

## Run summary

An initial `running` marker contains only `schema_version`, `run_id`, and
`status`. It is not a successful outcome or a heartbeat. A finalized record adds:

- `exit_code`: 0 success; 1 trial/requirement/sink failure; 2 source failure;
  130 cancellation. `status` is completed, failed, or cancelled.
- `summary.tasks[name]`: `scores[metric]` statistics and `trials`.
  `summary.trials` aggregates all admitted work.
- `sources[source_id]`: name, records_seen/accepted/rejected, exhausted, digest,
  provenance, and optional error. A non-exhausted digest identifies only a prefix.
- `requirements`: expression, actual numeric value or null, passed, reason.
- `failures`: bounded run-level failures, not a duplicated list of trial errors.
- `sinks`: name, status, successfully delivered event count, details, optional error.
- `output_dir`: selected directory or null.
- `info`: run ID, UTC start time, and per-task definition info, including schemas,
  resolved options, scorer names, and code provenance.

Every statistics object has count, mean, min, max. Empty observations have count
zero and null for the other fields. No percentiles or observation arrays are kept.
Trial outcomes completed/task_failed/scoring_failed/cancelled partition planned
(admitted trials). Scoring_skipped counts trials with an explicit null score and
can overlap any terminal outcome. Timing statistics omit phases never started;
total_ms excludes queue and sink waiting.

The source digest is SHA-256 of ordered normalized records serialized as compact,
sorted-key JSON plus a newline, excluding provenance and generated identity.
Labels are included. It is not a source-file hash. Code provenance records selected
distribution versions, source-module hash when available, and a framework hash
over Python package sources; it is not a full environment lock or wheel hash.

## Events

All events include `schema_version="mic-event-v1"`, `type`, and `source_id`.

- `case_accepted`: row_index, generated case_id, and normalized case
  (input, optional expected/metadata/label/provenance).
- `record_rejected`: row_index and safe error classification.
- `trial_finished`: task, row_index, trial, case_id, and result.
- `source_finished`: source summary, emitted after iterator cleanup.

Source indices count raw record occurrences, including rejections. Case IDs use
run/source/row occurrence; labels need not be unique. Trial numbers start at one.
Accepted records can exist without admitted trials if cancellation or an execution
cap intervenes. SourceFinished can precede completion of its admitted trials.

A trial result contains case_id, row_index, trial, status, input, scores, errors,
and latency. Expected, metadata, label, provenance, output, and task_metadata
appear only when present. Successful output validation is required before output
is recorded. Missing output is distinct from a valid null output.

Scores contain name, finite numeric or null value, and object metadata. Core scores
are not restricted to [0,1]. Error records contain phase, exception type, and a
generic message; scorer errors additionally name the scorer. Raw exception text,
SDK response bodies, locals, and tracebacks are not persisted. Timing fields
task_ms/scoring_ms/total_ms are nonnegative numbers or null.

Trial events follow completion order, not source order. Consumers join on
source/case/task/trial coordinates. The engine keeps no reorder buffer.

## Partial evidence and privacy

Event writes flush incrementally; final run publication uses atomic replacement
after all sinks have settled. A manifest write gets at most one recovery attempt,
without replaying tasks or sink writes. A failed recorder is disabled; other sinks
may still contain data. Files can be incomplete, truncated, absent, or stale after
disk failure or process interruption. A valid line/schema does not prove that the
entire run or an upload completed.

An in-memory result retains summary and sink outcomes, not lost case payloads.
Report generation rejects malformed/truncated event lines and unfinalized markers.
Default report caps are 10,000 trial records and 64 MiB combined input files; they
fail instead of silently producing a partial report.

Inputs, expected values, outputs, metadata, scores, labels, SQL, parameters, and
paths can all be sensitive. Generic error messages are not a general redaction
system. See [sharing guidance](reporting.md#sensitive-evidence-and-sharing).
