# Dataset factories and read context design

## Executive summary

Dataset factories will optionally receive `mic.ReadContext`, giving inline
generators the same reading capabilities as `DatasetSource.read(ctx)`. Read
configuration will use one object:

```python
mic.run(
    evaluation,
    limits=mic.ReadLimits(row_count=500, timeout_seconds=30),
)
```

Both constraints default to `None`. Row count means intentional selection of raw
source records; reaching it ends selection successfully. Timeout means cumulative
active reading and validation time, excluding task and sink backpressure.

Mic owns enforcement, counters, and cleanup. Providers can use the context to
reduce fetching and configure underlying SDK timeouts. This is the approved
implementation plan, based on main at `943be8b`; this document does not implement
the changes.

## Why

Today, only `DatasetSource.read` receives context. A simple generator must become
a class to publish source provenance or inspect read configuration. Factories
should have those capabilities without requiring that structural change.

The existing `max_rows` guard probes beyond the cap and fails an oversized read.
The intended everyday operation is instead to select a bounded prefix. Providers
should see that selection so they can avoid fetching unused records, for example
by constructing SQL with a `LIMIT` clause.

The existing read timeout defaults to 60 seconds and is tracked privately by the
dataset reader. It should be explicitly configured and expose its remaining
budget to providers.

## Public API and contracts

### Read configuration

```python
@dataclass(frozen=True)
class ReadLimits:
    row_count: int | None = None
    timeout_seconds: float | None = None
```

- `row_count` selects at most that many raw source records, including records
  subsequently rejected or skipped. Reaching it is a successful selection boundary.
- `timeout_seconds` bounds cumulative active reading and validation time.
- `None` means unconstrained for that dimension.
- Explicit row counts must be positive integers, excluding booleans.
- Explicit timeouts must be finite, positive numbers, excluding booleans.

This replaces `max_rows` and its overflow-error behavior. There is no additional
`limit` argument on run or preflight.

### Optional factory context

Existing factories remain valid:

```python
@mic.dataset(input=Ticket, expected=Classification)
def tickets() -> Iterator[object]:
    yield from local_records()
```

Factories can request context:

```python
@mic.dataset(input=Ticket, expected=Classification)
def tickets(ctx: mic.ReadContext) -> Iterator[object]:
    ctx.set_provenance(provider="sql", table="tickets")

    with connect() as connection:
        yield from fetch_tickets(
            connection,
            limit=ctx.limits.row_count,
            timeout=ctx.remaining_seconds,
        )
```

The application functions above are illustrative. The factory owns query
construction and can translate the selection into an SQL `LIMIT`.

Injection follows the existing task convention:

- Zero positional parameters: invoke without context.
- One positional parameter: pass context.
- Names and annotations do not determine injection.
- Reject additional positional parameters, variadic parameters, and required
  keyword-only parameters.
- Inspect signatures without invoking user code.
- Never retry an invocation after catching a user-body `TypeError`.

Regular functions, coroutine factories, generators, and async generators are
supported.

### Context shape

| API | Contract |
| --- | --- |
| `limits: ReadLimits` | Immutable effective configuration for this read. |
| `rows_seen: int` | Raw records consumed by Mic so far. |
| `remaining_rows: int \| None` | Remaining selection allowance, clamped to zero; `None` when unlimited. |
| `remaining_seconds: float \| None` | Remaining active read-time budget, clamped to zero; `None` when unlimited. |
| `set_provenance(**values)` | Merge validated JSON source evidence. |
| `provenance` | Return a defensive copy of accumulated provenance. |

Configuration and accounting properties are read-only. Provenance retains its
finite-JSON validation and cumulative 64 KiB bound. Mic increments `rows_seen`
after retrieving a raw record and before mapping or validation. When a generator
resumes after yielding a record, the updated count is visible.

`DatasetSource` remains useful for reusable, parameterized source configuration;
it provides no exclusive context capabilities. If a factory receives context and
returns a `DatasetSource`, Mic passes the same context instance to `read(ctx)`.
Their provenance updates accumulate together.

## Selection semantics

Mic checks the remaining allowance before requesting another record.

| Source size | Outcome with `row_count=500` |
| --- | --- |
| 300 records | Success; natural exhaustion observed. |
| Exactly 500 records | Success; selection complete, exhaustion unconfirmed. |
| More than 500 records | Success; selection complete, exhaustion unconfirmed. |

Mic does not request record 501 to distinguish the latter two cases. With 20
invalid records skipped within a selection of 500, there are at most 480 accepted
cases. Selection does not replenish rejected records.

For suites, the count applies per distinct dataset read. Evaluations sharing the
same dataset object share one read and context. Separate datasets receive
independent counters and budgets. Trials do not consume additional source
records. The separate `max_executions` control remains unchanged.

Providers may size requests using the remaining allowance:

```python
remaining = ctx.remaining_rows
if remaining == 0:
    return

page_size = (
    self.page_size
    if remaining is None
    else min(self.page_size, remaining)
)
```

Mic enforces selection centrally even if the provider ignores the context.

## Timeout semantics

Preserve the existing distinction between active reading and downstream waiting.
The budget includes factory invocation, source initialization and retrieval, and
mapping and validation. It excludes waiting for task execution or sink capacity.

`remaining_seconds` decreases during active operations and pauses between them.
It is a cumulative budget, not a continuously running wall-clock deadline. A
provider can recalculate before each operation:

```python
remaining = ctx.remaining_seconds
if remaining is not None and remaining <= 0:
    raise TimeoutError

request_timeout = (
    self.timeout
    if remaining is None
    else min(self.timeout, remaining)
)
```

Mic independently enforces the budget. SDK timeout and retry semantics determine
how well passing this value bounds a blocking operation. Avoid passing zero to
SDKs, where it can have unexpected meanings.

Cancellation remains cooperative. Mic waits for in-flight synchronous work before
closing resources; it cannot forcibly terminate a worker thread. Cleanup still
runs after the budget expires.

## Implementation walkthrough

### Normalize factory signatures

Extend factory typing to accept both call shapes:

```python
type SourceFactory = (
    Callable[[], object | Awaitable[object]]
    | Callable[[ReadContext], object | Awaitable[object]]
)
```

Add a factory adapter following `_contextual_task` in
[`decorators.py`](../src/mic/decorators.py). Preserve function metadata and
coroutine dispatch. Apply normalization consistently for decorated datasets and
direct `Dataset(...)` construction. Resolve model/context import cycles through
type-only imports and narrow internal interfaces.

### Create read state before invoking the factory

Currently, the factory executes before the source driver creates context. Change
the lifecycle in [`datasets.py`](../src/mic/_runtime/datasets.py) and
[`sources.py`](../src/mic/_runtime/sources.py):

1. Create read state and context.
2. Invoke the factory.
3. Initialize the returned source.
4. Retrieve, count, map, and validate records.
5. Close resources.
6. Capture final provenance and summary.

Use one serialized source worker for synchronous factory invocation, iterator
initialization, iteration, and cleanup. Async factory bodies and async iteration
execute on the event loop.

Preserve cancellation handling for factories finishing after cancellation: join
the operation and close any returned iterator. Capture provenance even if factory
or source initialization fails.

### Centralize selection

Replace the overflow check with a selection check. Illustrative private operations:

```python
while ctx.remaining_rows != 0:
    present, raw = await next_record()
    if not present:
        mark_exhausted()
        return
    record_consumed()
    yield raw

mark_selection_complete()
```

The driver must distinguish natural exhaustion from selection completion so
summaries remain accurate. Remove inspection's separate collection-and-break
logic; all entry points use this implementation.

### Share timeout accounting

Move the private remaining-time accounting from `DatasetReader._timed()` into
read state accessible through `ReadContext`. During an active operation, compute
the remaining budget from a monotonic clock:

```python
remaining = max(0.0, saved_budget - (now - operation_started))
```

Pause and save the budget when the operation ends. Retain the post-operation
timeout check for callbacks that finish after their allowance before the event
loop delivers cancellation.

### Update built-in providers

Braintrust will size pages with `remaining_rows`, remove extra-record probing,
and bound request timeouts by the remaining budget. Preserve pagination
validation, sanitized errors, and response cleanup.

BigQuery will remove `max_rows + 1` page sizing, use selection to reduce page size
where applicable, and recalculate timeouts before dry-run, query submission, and
result waiting. Preserve billing constraints and client ownership. Do not rewrite
arbitrary SQL automatically. Context enables readers owning query construction to
apply `LIMIT`; page sizing alone does not bound query scan cost.

Files and YAML retain generator-based resource ownership. Make the inline
context-taking YAML factory the primary custom-reader example, with a separate
class example for reusable configuration.

## Entry points and defaults

Run and preflight use `ReadLimits()` when omitted: no row selection and no read
timeout. Replace inspection's standalone `limit` argument with the same object:

```python
mic.inspect_dataset(tickets, limits=mic.ReadLimits(row_count=20))
```

Inspection remains a bounded preview. An omitted row count resolves to its
existing default of 20; an explicit positive count changes that size. The context
receives the effective count, including this default. An omitted timeout remains
unlimited.

For the CLI, replace `--max-rows` with `--limit` on run, preflight, and inspect.
Translate it into `ReadLimits.row_count`. Retain `--dataset-timeout`, defaulting to
no timeout. Run and preflight have no default row selection; inspect defaults to
20.

## Evidence and compatibility

Record effective configuration in run evidence:

```json
{"limits": {"row_count": 500, "timeout_seconds": 30}}
```

Keep `SourceSummary.exhausted` meaning natural exhaustion was observed. A
successfully selected prefix has `exhausted=false`; its digest describes the
accepted records within that prefix.

Breaking changes are replacing `ReadLimits.max_rows` with `row_count`, removing
overflow errors, defaulting the timeout to `None`, replacing inspection's
standalone `limit`, making context accounting read-only, and replacing
`--max-rows` with `--limit`.

Bump the run format to `mic-run-v4` because persisted read configuration changes.
Update schemas and readers directly, without backward readers or migration
layers. Retain the event format version if its defined structure remains
unchanged.

Update README, API/CLI/provider/artifact documentation, executable examples, and
the AGENTS.md statement that currently says limits always fail rather than select
a prefix. No runtime or integration dependencies are added.

## Validation strategy

| Risk | Validation |
| --- | --- |
| Incorrect factory injection | All sync/async forms, no-context factories, invalid signatures, direct construction, and no retry after user exceptions. |
| Different inline/class behavior | Equivalent records, provenance, counts, and cleanup behavior. |
| Off-by-one selection | Short, exact-length, oversized, and infinite sources; prove record N+1 is never requested. |
| Incorrect rejection accounting | Mapping failures and `RecordError` consume allowance under skip policy. |
| Shared-context mistakes | Shared datasets read once; distinct datasets have independent state. |
| Incorrect time accounting | Controlled clocks for factory/read/validation time, paused backpressure, and unlimited budgets. |
| Resource leaks | Cleanup after selection, initialization failure, timeout, and cancellation; join synchronous work before closing. |
| Provider regressions | Fake clients verify page sizes, timeout arguments, ownership, and pagination. |
| Misleading evidence | Schema validation and selected-prefix versus observed-exhaustion assertions. |
| Public API regressions | Typing fixtures, CLI defaults, validation, and executable documentation examples. |

After focused tests, run the full canonical verifier and build/clean-install
checks. No live provider access is needed. This plan-only PR does not claim
implementation verification; these checks are acceptance criteria for the code PR.

## PR strategy

Implement in one cohesive PR with focused commits. Factory context, selection,
timeout accounting, providers, and evidence describe the same contract; splitting
them into stacked PRs would introduce inconsistent intermediate behavior.

Suggested commits:

1. Read configuration, context state, and factory injection.
2. Shared lifecycle, selection, and timeout accounting.
3. Provider adaptations and controlled-transport tests.
4. Artifact version, CLI, documentation, examples, and full verification.

Implementation starts on a fresh `codex/` branch from updated main. This design
PR is the tracking reference for that implementation; it changes documentation
only.
