# Public API and execution contract

The top-level `mic` package contains ordinary authoring, execution, schema, result, and
error APIs. Advanced provider contracts are imported from `mic.providers`; reporter
contracts are imported from `mic.reporters`. Runtime materialization and artifact
implementation modules are private. The package contains `py.typed`, and the entire source
passes strict Pyright.

## Authoring

- `@dataset(name=..., schema=..., map_row=...)` binds a no-argument source factory.
  It can return a fresh synchronous/async iterable, an awaitable of either, or a
  provider handle. The factory is invoked once per run or inspection.
- `case_schema(input=..., expected=..., metadata=..., expected_policy=...)` accepts
  ordinary Python annotations or `Schema[T]` adapters. Metadata defaults to `JsonObject`.
  The dependency-free backend hydrates nested dataclasses from JSON mappings and
  revalidates/clones native instances. Maps require string keys; native schemas are
  strict and reject `strict=False`. Use explicit mapping for normalization.
  Optional Pydantic adapters preserve constraints and aliases through strict JSON
  hydration; canonical snapshots use Python field names. See [schemas](schemas.md).
- `@scorer(name=..., requires_expected=True)` accepts a synchronous or async
  function taking `ScoreContext[I,O,E,M]`. Each scorer defines exactly one metric,
  named by the scorer. Return a finite `float`/`int`, `None` when inapplicable, or
  `Score(value, metadata)` when the score needs JSON metadata.
- `@eval(name=..., dataset=..., output=..., scorers=[...], trials=1, concurrency=10)`
  accepts a task `(TaskContext[E,M], input: I) -> O | TaskResult[O]`, sync or async.
  `TaskResult` is the only metadata wrapper. Ordinary dictionaries containing
  `output` and `metadata` keys are not unpacked.
- Descriptors expose their stable `name`; callback storage is an implementation detail.
  Their constructors/decorators do not call provider SDKs or authenticate. Python module
  import is still ordinary code execution, not a sandbox.

## Presence, metadata and stable identity

Input must be present. Expected must be present by default; nullable schemas may
accept `None`. Set `expected_policy="optional"` for unlabeled data. A required
scorer rejects missing labels during preflight. `require_expected()` either returns
the typed expected value or raises `MissingExpectedError`. `Missing` is encoded as
an omitted field, while `None` is JSON null.

Absent/null metadata becomes absent. Supplied metadata must validate and serialize
to an object. Task metadata is shallow-merged onto the JSON projection of dataset
metadata with task keys winning; the result is hydrated through the metadata type
again. The artifact retains dataset metadata and task metadata separately.

IDs come from mapped IDs or provider record identity. When none exists, the fallback
combines normalized row content and its position. Explicit IDs must be unique after
normalization. Physical source information remains in provenance and is excluded
from the logical dataset digest. Row ordering is part of that digest.

Serializers must preserve a meaningful JSON round trip. Nonfinite numbers fail;
there is no automatic `repr`, pickle, or silent null substitution. Validation may
execute user validators, so validators and serializers should be pure. Dataset
validation runs once before trials; output and merged metadata are validated at
their own boundaries.

## Execution and errors

Blocking entrypoints use plain names: `run`, `preflight`, and `inspect_dataset`.
Async hosts use `await arun`, `await apreflight`, and `await ainspect_dataset`.
The run pair returns `RunResult(manifest, cases, output_dir, exit_code)`. Setup
failures raise typed `ConfigurationError`/`DatasetError`; when error artifacts are
successfully saved, the exception has a note pointing to its report. A dataset-snapshot
write failure returns exit 1 and saves an artifact-phase failure where the remaining
evidence files are writable. This also applies to filesystem initialization and
terminal manifest/report failures: computed cases remain in the returned result,
and persisted files may be incomplete or stale. Configuration/dataset errors and
cancellation retain their original exception when error persistence also fails;
secondary failures appear in exception notes. An existing nonempty output directory
is rejected without writing into it. Uninspectable schema adapters and non-callable scorers
fail before source access. Library code does not set process exit status. The CLI
translates results/errors into exit codes.

Configuration precedence is invocation arguments, decorated defaults, then library
defaults. There is no implicit `.env` loading.

All selected rows are read/validated before tasks start. `ReadLimits` defaults to
10,000 rows, 64 MiB serialized bytes, 1 MiB per record, and a 60-second source deadline.
The default maximum is 50,000 row/trial executions. Prefix inspection is an explicit
selection; caps fail instead of truncating. These are serialized-data limits, not
an exact heap quota, and output artifact size is not currently capped separately.

Work is admitted lazily to bounded workers. Every trial gets a fresh nested copy;
every scorer gets an independent copy of pristine input/expected and validated
output/merged metadata. Scorers run in declaration order. Final API/report ordering
is row index then one-based trial number; the on-disk case journal records completion
order. Earlier successful scores remain if a later scorer fails, and the case still
fails.

Async functions are awaited; synchronous functions run in a dedicated bounded
thread executor. Trial timeout covers task and scorers. On timeout/cancellation,
running sync callbacks are drained before releasing their slots; a hanging thread
cannot be forcibly stopped. Use async callbacks and SDK request deadlines when prompt
cancellation matters. Source reads and exports also clean up cooperatively.
Cancellation preserves partial evidence then re-raises `CancelledError`; the CLI
returns 130. No tasks or scorers are retried automatically.

## Reports and statistics

Every run writes `dataset.jsonl`, `cases.jsonl`, `run.json`, and `report.html`.
The manifest records input/expected/metadata and output schemas, selection, logical digest, source provenance, options,
source-module/framework hashes, versions, metrics, failures, and export status.
It intentionally omits environment dumps. Full evaluated values remain available.
Code provenance's framework hash covers Python sources throughout the `mic` package.
Reports embed case data, and exception text and provenance may be sensitive. Read
[sensitive evidence and persistence failures](reporting.md#sensitive-evidence-and-sharing)
before sharing reports or enabling remote export.

Scores are finite numbers or `None`; booleans are rejected. Numeric means exclude
null/unavailable values. Percentiles use TypeScript's nearest-rank convention.
Numeric, null and unavailable counts are reported for every scorer.
The CLI gate grammar is `metric >= number` (also `<=`, `==`, `>`, `<`); the argument
must be shell-quoted. Missing numeric values make a configured gate unevaluable
and therefore failing. Any execution error fails regardless of the numeric mean.

Optional reporters implement `name`, `async prepare()`, and
`async report(manifest, cases) -> JsonObject`. Preparation validates configuration;
reporting runs only after complete local artifacts exist. Reporters receive copies
so they cannot mutate local evidence. Export failure/cancellation is recorded
separately and never causes task replay.
Artifact failures prevent export; failure to save a reporter's outcome prevents
subsequent reporters from starting. The completed remote write is not undone or
repeated. In-memory results retain the known outcome when the filesystem cannot.
