# Mic v2 artifact contract

This reference describes the evidence written by the current alpha release. The
manifest identifies the format with `"schema_version": "mic-run-v2"`; it is not
the package version. Dataset and case rows inherit that version from their run.
There is no version field on each JSONL row.

## Files and schemas

| File | Meaning | Machine-readable schema |
| --- | --- | --- |
| `run.json` | Run identity, state, aggregates, failures, reporting outcomes | [Manifest](artifact-schemas/run-v2.schema.json) |
| `dataset.jsonl` | Normalized, reusable dataset snapshot in source order | [Dataset row](artifact-schemas/dataset-row-v2.schema.json) |
| `cases.jsonl` | One execution record per journaled trial, in completion order | [Case record](artifact-schemas/case-record-v2.schema.json) |
| `report.html` | Offline rendering embedding a manifest and case records | Uses the same manifest/case structures |

[Shared definitions](artifact-schemas/common-v2.schema.json) describe scores,
errors, statistics, options, and provenance. The JSON Schemas use Draft 2020-12.
They also provide named definitions for Mic-generated reporter failure/cancellation
outcomes and the built-in Braintrust result, without restricting custom results.
Their `$id` values identify documents, not a hosted validation service; resolve
references using the supplied local files. Enable date-time format checking when
validating timestamps. Tests do this without network retrieval.

These schemas describe Mic's current writers. They are validated in development,
not loaded during evaluation or enforced as a new runtime reader boundary. Mic's
report reader checks the run version and JSON structure; it does not perform full
JSON Schema validation. The schemas ship in the source distribution and repository,
not the dependency-free wheel.

All evidence must be finite JSON: strings, booleans, finite numbers, null, arrays,
and string-keyed objects. NaN and infinity are not JSON. Schema validators operating
on Python objects may need a separate finite-JSON check; the contract tests include
one. Mic-owned envelopes enumerate their fields; arbitrary values and extension
objects remain open. Embedded input/expected/metadata/output schema descriptions
are objects supplied by schema adapters, not independently executed by the artifact
validator.

The format is forward-only. An approved breaking format change increments the
version and replaces the supported shape. There are no backward readers or
migrations, and this alpha does not promise permanent format stability.

## Manifest fields

Every manifest has the following fields, including early setup records. The only
optional top-level field is `output_schema`, added when output-schema description
succeeds.

| Field | Shape and meaning |
| --- | --- |
| `schema_version` | Exactly `mic-run-v2` |
| `run_id`, `name` | Strings identifying this run and the evaluation |
| `status` | `running`, `completed`, `failed`, or `cancelled` |
| `started_at` | UTC ISO timestamp |
| `ended_at` | Null while running; UTC ISO timestamp for a terminal outcome |
| `duration_ms` | Nonnegative elapsed milliseconds; see timing below |
| `dataset` | Minimal `{name, rows: 0}` during setup, or the complete snapshot summary below |
| `options` | Empty before option resolution, otherwise `{trials, concurrency, timeout, max_executions}` |
| `output_schema` | Optional output-schema description object |
| `counts` | Nonnegative integer `planned`, `completed`, `failed`, and `cancelled` counts |
| `scores` | Metric names mapped to score statistics |
| `latency` | Empty before summarization; otherwise `task_ms`, `scoring_ms`, and `total_ms` statistics |
| `failures` | Array of error records; includes case errors and applicable run-level errors |
| `gates` | Array of `{expression, metric, actual, passed}` quality-gate outcomes |
| `reporting` | Reporter names mapped to their returned JSON objects or Mic-generated error outcomes |
| `artifacts` | Fixed names: `{dataset: "dataset.jsonl", cases: "cases.jsonl", report: "report.html"}` |
| `provenance` | Empty before collection, otherwise code identity described below |
| `exit_code` | `0`, `1`, `2`, or `130` |

Resolved options have positive integer `trials`, `concurrency`, and
`max_executions`. `timeout` is a positive finite number of seconds or null.

Code provenance has `python`, `package_version`, and `definition` strings,
`dependencies` (distribution names mapped to version strings), and
`framework_sha256` (64 lowercase hexadecimal characters). `source_path` and
`source_sha256` are present when a source-module file is available. The framework
hash covers package Python sources; it is not a digest of the installed wheel or
report templates. Dependency versions are selective, not a full environment lock.

### Status and failure interpretation

| Status | Exit code | Interpretation |
| --- | --- | --- |
| `running` | `0` | Intermediate record; not a success result |
| `completed` | `0` | Evaluation and applicable gates/reporters succeeded |
| `failed` | `1` | Execution, gate, artifact, or reporting failure |
| `failed` | `2` | Configuration or dataset setup error; library raises its typed exception |
| `cancelled` | `130` | Cancellation; library re-raises after owned work is cleaned up |

A failed gate has `passed: false` and can fail the run without adding an exception
to `failures`. `actual` is the metric mean, or null if unavailable. Reporter errors
are recorded under `reporting`; not every reporter error is also in `failures`.
Read status, gates, and reporting together rather than using a nonempty `failures`
array as the sole definition of failure.

Counts refer to executions, not unique dataset rows. After execution,
`planned = rows * trials`. On cancellation or journal failure, `cancelled` includes
unstarted executions as well as cancelled case records. A run cancelled during
export can have all cases completed. Early setup failures can have zero planned
executions even when a snapshot was already materialized.

Timing is measured through the start of the latest finalization, including completed
reporting work, but excluding that finalization's own writes. A saved `running`
record can remain after a crash or failed replacement; status is not a heartbeat.

## Dataset snapshot

The complete `dataset` summary has:

- `name`, `rows`, and `bytes`: name, row count, and canonical serialized byte count.
- `digest`: `sha256:` followed by 64 lowercase hexadecimal characters, computed
  over normalized rows serialized as sorted-key compact JSON plus a newline per
  row, encoded as UTF-8. It is a logical snapshot identity, not a source-file hash.
- `provenance`: an open provider-specific object, potentially containing SQL,
  parameters, paths, dataset identifiers, and other source details.
- `schema`: objects for `input`, `expected`, and `metadata`, plus `expected_policy`
  (`required` or `optional`).
- `selection`: `{limit: null}` for current run snapshots; dataset inspection may
  record a positive limit.

Each `dataset.jsonl` row requires a nonblank string `id` and an `input` value.
`expected` is optional and may be any JSON value, including null. `metadata` is an
optional object. Absent/null source metadata is omitted from the normalized row.
Source-specific per-case provenance is kept in execution records, not snapshot rows.
IDs are unique within a snapshot; generated IDs are based on normalized values and
source position. The byte count and digest cover the normalized rows including IDs.

For example, these are deliberately different records:

```json
{"id":"unlabeled","input":"hello"}
{"id":"explicit-null","input":"hello","expected":null}
```

## Case records

Each journal record describes one admitted execution:

| Field | Shape and meaning |
| --- | --- |
| `case_id` | Snapshot row's ID; replaces snapshot field `id` |
| `row_index`, `trial` | Zero-based snapshot row index and one-based trial number |
| `input` | Required normalized input |
| `expected`, `metadata` | Optional values copied from the snapshot, preserving omission |
| `status` | `completed`, `failed`, or `cancelled` |
| `output` | Present after successful output validation/serialization; any JSON value |
| `task_metadata` | Optional object supplied by `TaskResult` |
| `scores` | Array of `{name, value, metadata}` records |
| `errors` | Array of error records |
| `latency` | Nonnegative `task_ms`, `scoring_ms`, and `total_ms` numbers |
| `provenance` | Open per-case provenance object |

A completed case has an output and no errors. Failed/cancelled cases can retain
an output and earlier successful scores. Missing output is not equivalent to a
valid null output. A score `value` is a finite number or null, never a boolean;
score `metadata` is always an object. Core scores are not restricted to `[0, 1]`.
Task metadata is recorded separately from the original row metadata; scorers see
the validated merge described in the [API guide](api.md).

The case journal follows completion order. Returned in-memory cases and the initial
report are sorted by `(row_index, trial)`. Regenerating a report reads journal order.
Consumers should join by identity/coordinates, not assume line order equals row order.

### Statistics and errors

Every statistics object has `count`, `mean`, `min`, `max`, `p50`, and `p95`. `count`
is a nonnegative integer; the remaining fields are numeric, or all null when no
numeric values were observed. Percentiles use nearest rank. Score statistics add
`null_count` for explicit null scores and `unavailable_count` for executions that
did not produce that metric. Latency statistics summarize recorded executions.

Error records require `phase`, `type`, `message`, `row_index`, `case_id`, `trial`,
and `traceback`. Coordinates are null for run-level errors. `scorer` is an optional
scorer name. Current phases are `task`, `schema`, `scorer`, `cancelled`,
`configuration`, `dataset`, `artifact`, and `reporting`. Tracebacks are strings,
not structured stack-frame data, and their exact content is not stable across hosts.

## Reporter results

`reporting` is an open map of names to JSON objects. A custom reporter's successful
return is stored as-is; it need not include `status`. Mic's exception outcome is
`{status: "failed", error: <message>, type: <exception name>}`. Its cancellation
outcome is `{status: "cancelled", error: <message>}`.

The built-in Braintrust reporter returns `status: "completed"`, integer `rows`,
and `flushed: true`, plus optional string `url` and `experiment_id`. This describes
that reporter, not mandatory fields imposed on every extension.

## Partial evidence and privacy

Schemas validate individual records; they do not certify a complete transaction,
that an upload happened, or that a report is safe to share. A failed write can leave
an empty snapshot, a truncated journal tail, a missing report, or a stale manifest.
Do not force cross-file equality in those states or treat malformed journal tails
as valid case records. In-memory results can contain evidence absent from disk.

See [persistence and cancellation](reporting.md#persistence-failures-and-cancellation)
and [sensitive evidence](reporting.md#sensitive-evidence-and-sharing) for the exact
failure and sharing boundaries. No schema validation redacts private data.
