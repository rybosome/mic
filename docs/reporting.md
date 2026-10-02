# Reviewing a run

A run writes `run.json` (the versioned manifest), `dataset.jsonl` (the validated source
snapshot), `cases.jsonl` (one row per trial), and a self-contained `report.html`.
The HTML embeds the actual manifest and case rows. It requires no server, account,
network connection, third-party fonts, or JavaScript dependencies. Open it directly
in a normal browser. `mic report PATH` regenerates it without importing evaluation
definitions or invoking any tasks. Regeneration writes atomically and rejects
output paths that overlap the source manifest, dataset, or case journal, including
symlink and hardlink aliases. A failed replacement leaves the previous report intact.

The case inspector provides full-text search, execution/error/unscored filters,
case details, provenance, error tracebacks, copyable case IDs, and expandable raw
JSON. Arrow keys move between cases and between accessible tabs. Expected values
that are absent display **Missing**, while explicit JSON null displays **null**.
Numeric score means exclude null and unavailable scores; an execution error and a
zero score are different states. General finite scores are not assumed to be
percentages. An explicit `--require 'exact>=0.9'` is what turns low quality into a
failing process exit.

Dataset strings are escaped in embedded JSON and inserted using DOM text nodes.
A restrictive Content Security Policy permits only the report's hashed inline
script and inline styles. It blocks network requests and outside code. Raw
artifacts contain evaluated input/output content; they intentionally do not dump
the process environment.

## Sensitive evidence and sharing

For field definitions and machine-readable schemas, see the [artifact contract](artifacts.md).

Treat the run directory and HTML report as copies of your evaluation data. The
dataset snapshot contains inputs, expected values, metadata, and IDs. Case records
add outputs, task and score metadata, source provenance, and errors. The manifest
includes dataset provenance, code identity, and failures. Provenance can contain
absolute local paths, SQL text and parameter values, project/dataset identifiers,
and record IDs. Exception messages and tracebacks can include sensitive application
text, source lines, and filesystem paths.

The HTML embeds the complete manifest and case records. Search filters, collapsed
panels, and hidden fields do not remove data from the file. Offline operation and
the Content Security Policy prevent network access and executable data injection;
they do not redact or encrypt the report. Mic does not provide automatic redaction.
It cannot identify every secret in arbitrary user data or exception text.

Sanitize sensitive inputs, metadata, SQL parameters, and exception messages before
they enter an evaluation. Review the actual JSON and embedded HTML data before
sharing them. Keep run directories in access-controlled storage. The repository's
`.gitignore` excludes `.mic/`, but it does not protect custom output directories,
copied reports, or CI artifact uploads. Temporary evidence files may also contain
sensitive content; a filesystem fault can prevent their cleanup.

Enabling Braintrust export sends input/expected/output and scores, plus the full
case record under `metadata.mic.case` and dataset information under `metadata.mic`.
That includes case errors and provenance. Review this payload and the destination's
access controls before enabling export. Mic does not deliberately record provider
API keys or the process environment, but credentials placed in evaluation data or
user-generated exception messages become part of the evidence.

## Persistence failures and cancellation

Manifest, dataset snapshot, and report replacements use temporary files in the
destination directory. A failed write or replacement preserves any previous
complete destination file. These are atomic replacements of individual files,
not a transaction across the directory or a guarantee of durability after power
loss. The append-only case journal records completion order; an interrupted write
can leave an incomplete last record. Report regeneration rejects malformed records.

Terminal finalization attempts both the manifest and HTML independently. If either
fails, Mic records artifact failures in memory and makes one recovery attempt to
save the updated outcome. A persistent fault can leave files missing or stale,
including a previous manifest whose status is still `running`. The returned
`RunResult` retains computed cases and the known failures even when they cannot be
saved. The CLI warns that evidence may be incomplete or stale rather than claiming
a report was successfully written. A recovered write still leaves the run failed
so callers can see that an artifact fault occurred.

Filesystem initialization, snapshot, journal, and finalization failures produce
exit code 1. If Mic cannot verify or create an empty output directory, it returns
the failure in memory without attempting to write into that directory.
Configuration/dataset setup errors keep their typed exception and
exit code 2 even if saving the error also fails. Cancellation keeps exit code 130
and re-raises `CancelledError` after cleanup. Error notes identify secondary
persistence failures; they link to a report only after successful finalization.
Terminal outcomes include elapsed duration, including setup cancellation. Duration
is measured through the start of the latest finalization, including any completed
reporting work, but excludes that finalization's disk-write time.

An artifact failure prevents remote reporting from starting, even if recovery
succeeds. After each reporter, Mic saves its outcome. If those writes fail, later
reporters are not started. An export may already have completed remotely; the
known outcome remains in memory, but local files may not reflect it. Artifact
recovery never re-executes tasks or retries an export.

## Console and discovery

`mic list MODULE` imports only that selected Python module and inspects descriptors;
it does not call dataset factories. Python import itself executes top-level code,
so select trusted authoring modules. Exact `module:symbol` selectors avoid ambiguous
substring selection. The invocation directory is added to the import path so local
`examples.*` and your own modules work from the installed `mic` console command.

`mic inspect module:dataset --limit 3` selects an explicit prefix. Safety flags are
`--max-rows`, `--max-bytes`, `--max-record-bytes`, `--dataset-timeout`, and (for runs)
`--max-executions`. These are caps, not silent truncation. `mic preflight module:eval`
validates data and configuration without task calls or experiment writes.
`--timeout` on `run` applies to the entire trial, including task and scorers.

## Optional Braintrust export

Install `mic-evals[braintrust]` and set `BRAINTRUST_API_KEY`, then explicitly add
`--braintrust-project PROJECT` to `mic run`. Optional controls are
`--braintrust-experiment NAME`, `--braintrust-app-url URL`, and
`--braintrust-org ORGANIZATION`. The Python equivalent is
`BraintrustReporter(project=...)` from `mic.reporters.braintrust`.

`prepare()` checks configuration, SDK availability, and credential presence locally;
it cannot prove remote credentials or permissions. `report()` validates every score
before initializing an experiment, uploads the already-computed results, flushes batches of at most 100 cases, and requests a metadata-only summary for the experiment URL. It creates
a new experiment with `update=False` and never changes the SDK's current experiment.
A configured project name may cause the SDK to create that project if absent.

Braintrust numeric scores must be finite and in `[0,1]`; out-of-range values fail
export without modifying local scores. Null scores remain null. The installed SDK 0.39.0 strips top-level null
input/output/expected fields, so its native columns cannot distinguish absent and
explicit-null values. Each exported row's `metadata.mic.case` retains the exact full
local case, including presence semantics, dataset/task metadata, score metadata,
source IDs, and errors. A network-blocked memory-logger test exercises the actual
installed SDK and verifies this representation. Export uses top-level spans because the SDK's full-event
`Experiment.log()` rejects null input/output and incomplete failed cases. Local
latency appears in explicit `mic_*_ms` metrics; remote span duration measures export,
not model execution. There is no native scorer adaptation or telemetry replay.

The native reporter creates an isolated SDK `BraintrustState`, sets its owned
logger to synchronous flush mode, and disables the SDK's queue-drop behavior.
This is necessary because SDK 0.39.0's default background mode logs and drops
failed requests without reliably raising them to the caller. This version-sensitive
logger configuration is covered by local installed-SDK contract tests, including
a fake rejected HTTP upload that must raise. It does not modify the process's
current experiment or default logger. Keep the SDK pinned until this boundary is
re-verified on upgrade. SDK-managed background worker lifetime remains controlled
by the SDK.

Synchronous SDK `flush()` returning successfully is the observable success signal; this has not
been verified against an authenticated server in this environment. Failures that
propagate from init/log/flush are separate reporting failures and must not replay
tasks. Local artifacts remain authoritative. Cancellation during a synchronous SDK upload
waits for that thread to finish even after repeated cancellation requests; Python cannot kill
an active SDK thread. The remote experiment may already contain all or part of the
run. Cancellation never promises that the server undid an upload.

Implementation research: [Braintrust Python SDK reference](https://www.braintrust.dev/docs/sdks/python/versions/0.33.0)
and [official logger implementation](https://raw.githubusercontent.com/braintrustdata/braintrust-sdk-python/main/py/src/braintrust/logger.py).
Reporter tests cover both injected SDK doubles and the installed SDK. Neither
counts as an authenticated live check. SDK payloads are deep copies, so SDK
normalization cannot mutate local evidence. Logging/end failures retain their
original error, record secondary cleanup failures as notes, and attempt a final
flush of earlier queued spans before returning.

## Cloud equivalence demo

`examples.provider_equivalence:bigquery_fixed` uses a literal, ordered SELECT of
the same three offline cases. Set `MIC_BIGQUERY_PROJECT` and optionally
`MIC_BIGQUERY_LOCATION`; authentication follows Application Default Credentials.

For `examples.provider_equivalence:braintrust_fixed`, prepare an existing fixture
whose input/expected match `examples/fixtures/triage.jsonl`. Store the logical case
ID in `metadata.case_id` for each row. Set `MIC_BRAINTRUST_DATASET_ID` and the pinned
`MIC_BRAINTRUST_XACT_ID`. The example's explicit mapper removes this transport-only
metadata key, maps it to the case ID, and retains physical record ID in provenance.
No fixture is created or modified by these examples. Source row ordering must also
match when comparing snapshot digests; the same cases in a different order produce
a different ordered snapshot.

## Verification

Acceptance tests exercise gates, async trials, invalid selectors, resource caps,
report regeneration, escaping, CSP construction, SDK upload failures, and mutation
isolation. Ten jsdom tests execute the actual report script for rendering,
search/filter behavior, keyboard navigation, empty states, notices, and clipboard
fallbacks. See the [verification guide](verification.md) for commands and remaining
manual browser checks.
