# Public API and execution contract

The top-level `mic` package contains ordinary authoring, execution, schema, result, and
error APIs. Sources implement `mic.DatasetSource`; event/sink contracts are imported from
`mic.sinks`. Runtime implementation modules are private. The package contains `py.typed`, and the entire source
passes strict Pyright.

## Authoring

- `@dataset(input=..., expected=..., metadata=..., expected_policy="required", map_row=...)`
  binds a no-argument source factory. Metadata defaults to `JsonObject`.
  Alternatively, pass `schema=case_schema(...)` to reuse a composed schema; do not
  combine `schema=` with any of the four individual schema fields.
  It can return a fresh synchronous/async iterable, an awaitable of either, or a
  provider handle. The factory is invoked once per run or inspection.
- `case_schema(input=..., expected=..., metadata=..., expected_policy=...)` accepts
  ordinary Python annotations or `Schema[T]` adapters. Metadata defaults to `JsonObject`.
  The dependency-free backend hydrates nested dataclasses from JSON mappings and
  revalidates/clones native instances. Maps require string keys; native schemas are
  strict and reject `strict=False`. Use explicit mapping for normalization.
  Optional Pydantic adapters preserve constraints and aliases through strict JSON
  hydration; canonical records use Python field names. See [schemas](schemas.md).
- `@scorer(requires_expected=True)` accepts a synchronous or async
  function taking `ScoreContext[I,O,E,M]`. Each scorer defines exactly one metric,
  named by the scorer. Return a finite `float`/`int`, `None` when inapplicable, or
  `Score(value, metadata)` when the score needs JSON metadata.
- `@eval(dataset=..., scorers=[...], trials=1, concurrency=10)`
  accepts a task `(input: I) -> O | TaskResult[O]` or
  `(TaskContext[M], input: I) -> O | TaskResult[O]`, sync or async.
  `TaskResult` is the only metadata wrapper. Ordinary dictionaries containing
  `output` and `metadata` keys are not unpacked.
- All three decorators accept optional `name=`. If omitted or `None`, it defaults
  to the function's `__name__`, not a module-qualified name. Use `@scorer()` with
  parentheses. Callables without a function name (such as `functools.partial`)
  need an explicit name. Explicit names are unchanged; empty names are not replaced.
  CLI selectors remain `module:symbol`. Scorer names must be unique within an
  evaluation; use explicit names to disambiguate or retain a metric name after a rename.
- Descriptors expose their stable `name`; callback storage is an implementation detail.
  Their constructors/decorators do not call provider SDKs or authenticate. Python module
  import is still ordinary code execution, not a sandbox.

### Output schema inference

When `output=` is omitted, `@eval` resolves the task's return annotation at
decoration time and compiles it using the same schema backend as explicit types.
It never calls the task or samples a returned value. The inferred schema is
available to preflight and validates every output during execution.

```python
@mic.eval(dataset=tickets, scorers=[accuracy])
def classify(ticket: Ticket) -> Classification:
    ...
```

Sync and async functions use the annotated result type. `TaskResult[T]`,
`Awaitable[T]`, and `Coroutine[..., T]` wrappers are unwrapped; supported unions
retain their alternatives, including `None`. Input and expected types do not
determine the output type, and scorers are not used for runtime schema inference.

Explicit `output=Type` or `output=schema_adapter` remains supported and bypasses
return-annotation inference. Use it for unannotated functions, unresolved local
forward references, generic return types, or custom validation adapters.
Missing/unresolvable/unsupported return annotations and inferred `Any`/`object`
fail with a configuration error directing the author to `output=`. No fallback
to the expected schema or a permissive schema occurs.

String annotations resolve in the function's global namespace. Define referenced
types before the decorated function; enclosing function locals are not searched.
Annotation evaluation is ordinary trusted Python execution. Only the return
annotation is resolved for inference; unrelated parameter annotations are not.
Static typing still checks task/scorer/dataset compatibility, including evaluations
with no scorers. For advanced schema capabilities, see [schemas](schemas.md).

## Callback signatures and context types

Use an input-only task unless it needs execution context:

```python
def classify(ticket: Ticket) -> Classification:
    ...


def classify_with_context(
    ctx: mic.TaskContext, ticket: Ticket
) -> Classification:
    ...
```

These are alternative callback signatures for the same `@mic.eval(...)` decorator.
At decoration time Mic inspects the signature once: one positional parameter receives
input; two receive context then input. Parameter names and annotations do not select
the calling convention. Both positional-only and ordinary positional parameters are
supported, including positional parameters with defaults; Mic supplies every positional
parameter. Optional keyword-only parameters keep their defaults. Required keyword-only
parameters, `*args`, `**kwargs`, and other positional arities are rejected immediately
with `ConfigurationError`. Bound methods, partials, and decorators that preserve their
signature with `functools.wraps` are supported when their exposed signature fits these rules.
Uninspectable signatures are rejected. Mic never calls a task to probe its signature or
retries it after `TypeError`. Sync callbacks retain bounded thread execution; async
callbacks are awaited, including awaitables returned by synchronous functions.

Both forms retain static input/output checks, output validation, and `TaskResult`
metadata support. Input-only tasks receive no context or expected answer. Context-aware
tasks can access case ID, trial, and metadata. `TaskContext` has no reference
answer; expected values are available only to scorers.

Context type parameters have defaults:

| Annotation | Equivalent full annotation |
| --- | --- |
| `TaskContext` | `TaskContext[JsonObject]` |
| `ScoreContext[I, O]` | `ScoreContext[I, O, O, JsonObject]` |
| `ScoreContext[I, O, E]` | `ScoreContext[I, O, E, JsonObject]` |

Use `TaskContext[M]` or `ScoreContext[I, O, E, M]` for custom metadata.
`E` is the expected type, not necessarily the output type; `M` is case metadata.
The shorthands work on Python 3.12+ without adding core runtime dependencies.
`@mic.scorer()` defaults to `requires_expected=True`; opt out explicitly
for reference-free scorers. `require_expected()` returns the typed reference value or
raises if missing; `ScoreContext.expected` includes `Missing` in its type.

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

Case identity is generated from run ID, source occurrence, and zero-based row index.
A supplied `RawCase.id` becomes a label, not a uniqueness constraint. Duplicate
labels are accepted without an unbounded seen-ID set. Repeated trials share a
case ID; use task name and one-based trial number to identify an execution.
Source digests cover ordered normalized records, including labels but excluding
physical provenance and generated IDs. A non-exhausted source has a prefix digest.

Serializers must preserve a meaningful JSON round trip. Nonfinite numbers fail;
there is no automatic `repr`, pickle, or silent null substitution. Validation may
execute user validators, so validators and serializers should be pure. Dataset
validation runs once before trials; output and merged metadata are validated at
their own boundaries.

## Execution and errors

`run(spec, *, output=None, trials=None, concurrency=None, require=(), sinks=(),
limits=None, max_executions=50_000, timeout=None, on_invalid="abort")` returns
a compact `RunResult`. Use `await arun(...)` in an async host. Neither retains
case results or writes files by default.

```python
result = mic.run(
    classify,
    concurrency=2,
    on_invalid="skip",
    require=['tasks["classify"].scores["accuracy"].mean >= 0.9'],
)
print(result.summary.tasks["classify"].scores["accuracy"])
print(result.sources)
raise SystemExit(result.exit_code)
```

The result contains run identity/status/exit code, `summary`, source summaries,
requirement outcomes, bounded run-level failures, sink receipts, optional output
directory, and definition/configuration info. `to_json()` projects it into finite
JSON. Per-trial evidence goes only to selected sinks, never an implicit case list.

Configuration errors (invalid schemas, options, requirements, duplicate sink names,
nonempty output directory) fail before reading sources. Streaming source errors
instead produce a failed result with exit code 2 and preserve already-admitted work.
Task/scorer/requirement/sink failures produce exit code 1. Cancellation joins owned
work, finalizes selected sinks, then re-raises `CancelledError`; the CLI returns 130.
Library code does not terminate the process.

Each record is read, mapped, validated, and admitted before the next bounded read.
Tasks and scoring are pipelined per trial, not separate dataset passes.
A queue and worker pool are both bounded by concurrency. Slow sinks apply
backpressure. Storage scales with active records, configured definitions, and
provider page size, not total dataset size. Provider SDKs and custom sinks can
have their own allocations; this is not a precise process-memory quota.

Every trial gets isolated input/context. Each scorer gets independent copies of
pristine input/expected and validated output/merged metadata. Scorers run in
declaration order; one ordinary scorer failure does not prevent other scorers
from running. Completed events follow completion order, with no reorder buffer.

`on_invalid="abort"` stops source admission at the first malformed record.
`"skip"` emits a rejection and continues for row mapping/schema errors or explicit
`RecordError` events. Iterator exceptions, resource limits, and transport failures
remain fatal. An empty/all-rejected evaluation fails. Source failure does not
cancel previously admitted trials. Side effects already performed are not undone.

`ReadLimits` defaults: 10,000 raw records, 64 MiB normalized bytes, 1 MiB per
normalized record, and 60 seconds cumulative active reading/mapping time.
Providers also account raw bytes where available. Time spent waiting for task or
sink capacity does not consume the source budget. Caps fail visibly, never
silently truncate. `max_executions` caps admitted trials, not an estimated source size.

Sync callbacks run on bounded threads. Trial timeout covers task, output validation,
and scorers; it excludes queue/sink time. Timings include cooperative cleanup.
Running synchronous code cannot be forcibly killed: timeout/cancellation waits
for it to return before releasing resources. Use SDK request deadlines. Mic does
not retry tasks, scorers, or sink writes. There is no implicit `.env` loading.
Invocation options override decorated defaults.

`preflight` / `apreflight` drain and validate the source without tasks, retaining
only counts and provenance. They raise `DatasetError` on invalid data and do not
cache anything for a later run. `inspect_dataset` / `ainspect_dataset` intentionally
collect a bounded prefix (default 20 records; explicit positive `limit` supported).

## Multiple evaluations

Pass a sequence to the same runner:

```python
result = mic.run(
    [baseline, candidate],
    concurrency=4,
    require=[
        'tasks["candidate"].scores["accuracy"].mean >= 0.9',
        'trials.task_failed == 0',
    ],
)
```

All evaluation names must be unique. Repeated references to the same evaluation
are deduplicated. A heterogeneous selection can be annotated as
`Sequence[mic.EvaluationDefinition]`; its members remain ordinary fully typed
`Evaluation[I, O, E, M]` descriptors. Dataset/scorer/callback checks still happen
at authoring time, before this dispatch boundary.

Evaluations referencing the **same Dataset object** share one factory call and
validated stream. Equality of names, handles, or SQL does not imply sharing.
Every subscriber receives isolated trials; one task's mutation cannot affect
another. Expected answers are required for a shared source if its schema or any
subscriber's scorer requires them.

Distinct source groups are opened sequentially in first-selected order; admitted
work can overlap the next source. A source failure stops its group, not independent
groups. Global execution caps or sink failure stop admission across the selection.
An empty/all-rejected selected task fails rather than hiding behind another task's
success.

`concurrency=` is one **global** worker/queue bound, not multiplied by task count.
When omitted, the largest selected decorator concurrency supplies that global
bound; decorators do not impose additional per-task quotas. `trials=` overrides
all tasks, otherwise each keeps its own default. `max_executions` is global;
`ReadLimits` apply separately to each source group. Results/requirements use
evaluation names and global trial aggregates.

`preflight` remains a single-evaluation data-validation operation, not a suite
cache. CLI `run module:*` selects all public evaluations in sorted symbol order,
deduplicating aliases; exact selectors are unchanged.

## Bounded summaries and requirements

`Statistics(count, mean, min, max)` contains numeric observations only. No
percentiles are retained. Empty statistics have count 0 and all other fields
`None`. `TaskSummary` groups `scores` and `trials`; `EvaluationSummary` groups
`tasks` by evaluation name and global `trials`. Mappings are copied and read-only.

`TrialSummary` fields:

- `planned`: admitted commitments, not an estimate of unread rows.
- `completed`, `task_failed`, `scoring_failed`, `cancelled`: exclusive terminal
  outcomes whose sum equals planned after cleanup.
- `scoring_skipped`: trials with at least one explicit `None` score; overlaps
  terminal outcomes. It does not count scorers which never started.
- `task_ms`, `scoring_ms`, `total_ms`: statistics over observed phase timings;
  phases never started are omitted. Global means weight individual observations.

Requirements use these same fields, for example
`tasks["classify"].scores["accuracy"].min >= 0.7` or
`trials.scoring_ms.max <= 10000`. The closed grammar accepts explicit field/key
access and one comparison against a finite number, never Python evaluation.
Unknown task/metric names are rejected before effects. `None` fails every
comparison, including `!=`. Multiple requirements are ANDed. Passing requirements
cannot override execution or source failures. See [CLI gates](cli.md#quality-gates).

## Optional results and evidence

`output=".mic/run"` opts into a JSONL event recorder and atomic final `run.json`.
The directory must be empty or absent. `mic report .mic/run` renders recorded
trials afterward with explicit size caps; HTML is never generated automatically.

Custom sinks implement `ResultSink.open(RunInfo)` as an async context manager
yielding a `SinkSession` with `write(RunEvent)` and
`finish(EvaluationOutcome) -> SinkReceipt`. Calls are serialized, awaited, and
isolated with copied data. Cleanup occurs on success, failure, and cancellation.
A failed sink is disabled; healthy sinks finalize. The outcome passed to
`finish` describes computation, not the not-yet-known outcome of all sink cleanup.
The final `RunResult` incorporates every sink receipt.

See [sink lifecycle and Braintrust](reporting.md), [artifact schemas](artifacts.md),
and [source authoring](providers.md#custom-sources).
