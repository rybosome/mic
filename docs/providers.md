# Dataset sources

Dataset factories return passive handles or fresh Python iterables. The dataset
schema and optional `map_row` function define evaluation cases; a source does not
choose the task, output schema, scorer, or reporting destination. Built-in and external sources implement
exactly the same public `mic.DatasetSource` contract. No registration is needed. Importing their modules does not
initialize cloud SDKs or discover credentials.

The core package has **no runtime dependencies**. Native dataclass and Python
type schemas work without Pydantic. Optional extras are deliberately narrow:
`mic-evals[bigquery]` declares only `google-cloud-bigquery`,
`mic-evals[braintrust]` declares only the pinned `braintrust==0.39.0`, and
`mic-evals[pydantic]` declares only Pydantic. Dependencies those SDKs themselves
require are installed transitively. HTTPX is a development/test dependency, not a
dependency of the core or the Braintrust extra.

The implemented sources are memory, JSON/JSONL files, BigQuery, and Braintrust.
Cloud adapters have contract tests and optional-SDK compatibility checks. Live
verification requires explicit fixture configuration; skipped tests are not live
verification evidence.

Source provenance is persisted evidence and may include paths, SQL and parameters,
dataset identifiers, and record IDs. Keep credentials out of these fields and out
of user mapper exceptions. See [sensitive evidence and sharing](reporting.md#sensitive-evidence-and-sharing)
for what is embedded in reports and sent during optional export.

## Shared limits and identity

`ReadLimits(row_count=None, timeout_seconds=None)` has no default row selection or
read timeout. A configured row count selects that many raw records, including
rejections. Selection succeeds without requesting an extra record; it does not
claim the source was exhausted. Inspection resolves an omitted row count to 20.
Use `inspect_dataset(..., limits=ReadLimits(row_count=N))` or `mic inspect --limit N`.

The optional time budget includes active factory, reading, and mapping work and
excludes task/sink backpressure. There is no total dataset or per-record byte
limit: each record and parser/SDK buffers must fit in memory. Providers may impose
their own input-size policies. Inspection is blocking; async hosts use
`await ainspect_dataset(...)`.

Rows normally contain `input`, optional `expected`, optional object-shaped
`metadata`, and optional string `id`. Missing `expected` is distinct from an
explicit JSON `null`. Dataset schemas require expected values unless configured
with `expected_policy="optional"`. Custom mappers return `RawCase` and can map
arbitrary source columns to that envelope. Decode source-specific JSON string
columns explicitly in that mapper so normalization remains visible in the definition.

IDs supplied by a provider or mapper become labels; duplicate labels are allowed.
Machine case IDs identify run/source/record occurrence, with no global uniqueness set.
Incremental digests include ordered normalized inputs, expected values, metadata,
and labels, excluding provider provenance and generated identity. Cross-provider
equality therefore needs equal logical labels and ordering. Exhaustion is reported
separately; a prefix digest does not claim completeness. Braintrust physical record IDs remain in per-case provenance even
when a mapper supplies a separate logical ID; see
[`examples/provider_equivalence.py`](../examples/provider_equivalence.py).

## Python iterables

A factory may return a fresh list, generator, or async iterator. Use `RawCase` to
pass native dataclasses or Pydantic models. Iterators are consumed only once per
read, and active generators are closed when reading ends early or fails. Reusable
containers themselves are untouched. Return a fresh iterator each time instead of
capturing a generator that a previous run already exhausted.

Sync iteration and blocking SDK operations use worker threads. Python cannot
forcibly terminate a thread; cancellation joins an in-flight source read before
closing its resources. An uncooperative user iterator can consequently outlive a
nominal deadline. Use async sources or operations with their own timeouts when
hard responsiveness matters.

## Local files

```python
from pathlib import Path
from mic.providers.files import JSONFileHandle, JSONLFileHandle

JSONLFileHandle(Path("fixtures/triage.jsonl"))
JSONFileHandle("fixtures/cases.json")
JSONFileHandle("fixtures/export.json", records_key="items")
```

Both handles accept `Path` or string and read their declared format regardless of
filename suffix. `JSONLFileHandle(path)` reads one JSON object per nonblank line,
including `.ndjson` files. `JSONFileHandle(path, *, records_key=None)` reads a
JSON array one record at a time, without loading the whole array.

Set `records_key="items"` for a document such as:

```json
{
  "version": 1,
  "items": [{"input": "ticket", "expected": "bug"}],
  "description": "Ticket classification cases"
}
```

The key is an exact top-level member name, including literal dots or an empty
string; it is not a path expression. The selected value must be an array of
objects. Empty arrays are valid. Other fields can precede or follow it and are
validated and discarded incrementally. There is no automatic key detection or
whole-document mapper. Without `records_key`, the document must be an array.

Use the dataset's `map_row` for exports whose records have different columns.
The reader preserves custom columns and adds physical provenance; a custom mapper
must forward that provenance if it wants to retain it:

```python
import mic
from mic.providers.files import JSONFileHandle


def map_export(row: object) -> mic.RawCase:
    if not isinstance(row, dict):
        raise TypeError("Expected an export record")
    return mic.RawCase(
        input=row["question"],
        expected=row["answer"],
        provenance=row["provenance"],
    )


@mic.dataset(input=str, expected=str, map_row=map_export)
def exported_cases() -> JSONFileHandle:
    return JSONFileHandle("export.json", records_key="items")
```

Malformed JSON, duplicate object keys, nonfinite numbers, non-object rows, and
invalid source provenance are record errors. Recoverable framed records can be
skipped with `on_invalid="skip"`; broken framing aborts. Wrapper syntax errors,
duplicate wrapper keys, a missing selected key, and a non-array selected value
are source errors and cannot be skipped. Wrapper errors omit source contents.
Physical file path and one-based source row number are added to each row's
`provenance`; JSONL also adds its one-based physical line number. Read-level
provenance includes the selected `records_key` when supplied.

Each selected record must fit in memory. Wrapper traversal retains individual
scalar values and the keys of active objects to detect duplicates, but does not
retain unselected arrays or object values. Unselected wrapper values have a
maximum nesting depth of 128 container levels. These limits apply independently
of the selected records' existing JSON decoder limits.

A complete file read records `raw_sha256`, `raw_bytes`, and
`read_complete=true`. A selected prefix records `raw_prefix_sha256` and does not
claim source exhaustion or validate the unread suffix. JSON parser read-ahead is
included in the consumed-byte digest; it need not end at the selected row's exact
boundary. `read_complete` means the underlying reader observed EOF, independently
of whether iteration validated the entire document. Handles open a fresh file per
read and close it on exhaustion, failure, or early exit.

**Breaking API change:** use `JSONLFileHandle(path)` or `JSONFileHandle(path)` in
place of the former generic file handle. Format is now explicit in the class name.

## BigQuery

Install the `bigquery` extra. The default SDK client uses Application Default
Credentials and the handle's billing project and location. No service-account
file search or credential discovery is performed by mic itself.

```python
from mic.providers.bigquery import BigQueryHandle, BigQueryParameter

source = BigQueryHandle(
    billing_project="eval-project",
    location="US",
    sql="""SELECT case_id AS id, input_json, expected_json, metadata_json
           FROM `eval-project.evals.triage`
           WHERE team = @team
           ORDER BY case_id""",
    parameters=(BigQueryParameter(name="team", sql_type="STRING", value="support"),),
    maximum_bytes_billed=100_000_000,
    page_size=1000,
    timeout=60.0,
)
```

Required fields are `billing_project`, `sql`, and `location`; remaining defaults
are shown above. `parameters` defaults to an empty tuple. Parameters accept the
named Standard SQL scalar types supported by this adapter (`STRING`, `INT64`,
`FLOAT64`, `BOOL`/`BOOLEAN`, `BYTES`, `DATE`, `DATETIME`, `TIME`, `TIMESTAMP`,
`NUMERIC`, `BIGNUMERIC`, `JSON`) and arrays such as `ARRAY<INT64>`. Values must be
finite JSON values; dates and decimals may use SDK-supported string forms.
Nested STRUCT parameters are not implemented.

The source performs a dry run with Standard SQL and query cache disabled, checks
the server's statement type is exactly `SELECT`, and rejects estimated bytes over
the cap before submitting the executable query. Scripts, DML, and DDL fail this
check. The execution also carries `maximum_bytes_billed`. SQL identifiers belong
in explicit SQL configuration; use parameters for values. A `LIMIT` clause is a
row selection and is not a BigQuery scan-cost guarantee. Supply a deterministic,
unique ordering key; the adapter does not attempt to prove SQL ordering.

Rows remain raw column mappings, including SDK-native datetime/Decimal/bytes
values, for your mapper. A returned row with no column mapping is a recoverable
record rejection; SDK/transport exceptions remain fatal. Provenance includes SQL, named parameter values, billing
project/location, a stable query identity hash, dry-run estimate, job ID, and
available actual processing/billing/cache metadata. Query values are evidence;
do not put credentials in SQL parameters.

There is no separate estimation API or CLI command. The mandatory dry run is an
execution safety check. Supply `client=` on the handle to retain caller ownership,
or `client_factory=` to transfer client ownership to the source. `config_factory(handle, dry_run)` supports SDK-free fake
client tests. Owned clients close on success, error, and cancellation; query
cancellation is best effort.

References: [QueryJobConfig](https://docs.cloud.google.com/python/docs/reference/bigquery/latest/google.cloud.bigquery.job.QueryJobConfig),
[query parameters](https://docs.cloud.google.com/bigquery/docs/parameterized-queries),
[Application Default Credentials](https://cloud.google.com/docs/authentication/application-default-credentials).

## Braintrust

Install the `braintrust` extra. The source uses the pinned Braintrust SDK's owned
HTTP session and the read-only BTQL endpoint; it never calls `init_dataset`,
creates a dataset, or writes experiments.
Experiment export is the independent `BraintrustSink` integration.

```python
from mic.providers.braintrust import BraintrustHandle

source = BraintrustHandle(
    dataset_id="existing-dataset-id",
    xact_id="pinned-xact-id",
    api_url="https://api-eu.braintrust.dev",  # Optional explicit data plane.
    page_size=100,
    timeout=30.0,
)
```

`dataset_id` and `xact_id` are required. Empty transaction IDs and moving labels
`latest`, `head`, and `main` are rejected. Obtain the exact dataset `_xact_id`
from the fixture owner. `api_url` defaults to `BRAINTRUST_API_URL`, then
`https://api.braintrust.dev`. Explicit handles support EU or self-hosted data
planes. URLs require HTTPS, except localhost HTTP for tests.

Set `BRAINTRUST_API_KEY`, or supply `BraintrustHandle(..., api_key=...)`. Read settings
only when opening the dataset. The default source client is owned and closed by
the source. Injected clients implement the small async `ReadClient` protocol and
remain caller-owned; an existing `httpx.AsyncClient` satisfies that protocol.
The `transport=` convenience parameter still supports explicit HTTPX mock
transports when HTTPX is separately installed, and creates a source-owned client.
Production defaults never import HTTPX. The API key does not enter dataset
provenance and is passed explicitly without a fallback to unrelated `.netrc`
credentials.

The pinned SDK's high-level dataset fetcher buffers all pages, and its legacy
long-lived HTTP adapter can eagerly read entire response bodies. To stream response data, the adapter uses the ordinary session owned by `BraintrustClient` with
`stream=True`, redirects disabled, and an explicit request timeout. Blocking
request/read operations run in worker threads. Cancellation joins the in-flight
read before closing its response and client. This low-level SDK bridge is covered
by tests against 0.39.0 and is a compatibility point to recheck on SDK upgrades.

Each `/btql` request has a structured query selecting existing dataset rows in
`_pagination_key` order, a bounded page limit, `fmt="jsonl"`, and the pinned
top-level `version`. The reader streams response bytes and reads cursors from
`x-bt-cursor` or `x-amz-meta-bt_cursor`. The documented cursor flow ends at an
empty page without a cursor. A nonempty page missing its cursor, an immediately repeated
cursor, an empty page with a cursor, or a response exceeding its requested page
limit fails explicitly while reading the selected records. The adapter sizes
pages to the remaining selection and stops without an extra request once that
selection is consumed. Cursor tracking uses constant space. Longer cursor cycles
are bounded only when a row selection or read timeout is configured; the adapter
does not retain every historical cursor.

Malformed JSONL records, missing inputs, and missing physical IDs yield a
recoverable record error at the known line boundary. Run policy decides abort or
skip; resource limits and pagination/transport failures always abort the source.

Records preserve input, expected presence/null, metadata, and physical record ID.
Provenance retains dataset ID, pinned version, physical record ID, and returned
transaction/pagination fields. Attachment descriptors are preserved as data;
the source does not download attachment contents.

The fetch-dataset endpoint is deliberately unused: its documented pagination may
return older versions of previously returned IDs. The BTQL path instead requests
the pinned, deduplicated source snapshot. Actual multi-page behavior remains a
live integration gate for the target data plane.

References: [Braintrust API reference](https://www.braintrust.dev/docs/api-reference),
[BTQL request/response schema](https://www.braintrust.dev/docs/kb/btql-post-endpoint-payload-and-response-schema),
[SQL cursor and version semantics](https://www.braintrust.dev/docs/reference/sql),
[fetch-dataset pagination caveat](https://www.braintrust.dev/docs/api-reference/datasets/fetch-dataset-post-form).

## Verification

The provider suite is offline by default, even with extras installed:

```sh
uv run pytest tests/providers/test_file_memory_providers.py \
  tests/providers/test_bigquery_provider.py \
  tests/providers/test_bigquery_sdk_transport.py \
  tests/providers/test_braintrust_provider.py \
  tests/providers/test_braintrust_sdk_transport.py \
  tests/providers/test_provider_equivalence.py \
  tests/packaging/test_dependencies.py
```

Real source tests use fixture-owner-supplied environment configuration. They do
not create fixtures or discover credentials. The BigQuery SQL must select at
least two small, ordered rows; the Braintrust pinned dataset must contain at least
two records. Each live test sets page size to one to exercise multiple pages.

```sh
# Set these explicitly in a credentialed environment, then run the chosen test:
export MIC_LIVE_BIGQUERY=1
export MIC_BIGQUERY_PROJECT="fixture-billing-project"
export MIC_BIGQUERY_LOCATION="US"
export MIC_BIGQUERY_SQL="SELECT * FROM fixture_table ORDER BY id"
export MIC_BIGQUERY_MAX_BYTES="1000000"
uv run --extra bigquery pytest tests/integration/test_live_providers.py \
  -k bigquery --junitxml=.mic/bigquery-live.xml

export MIC_LIVE_BRAINTRUST=1
export MIC_BRAINTRUST_DATASET_ID="existing-fixture-id"
export MIC_BRAINTRUST_XACT_ID="exact-fixture-xact-id"
# BRAINTRUST_API_KEY and optional BRAINTRUST_API_URL supplied by your environment.
uv run --extra braintrust pytest tests/integration/test_live_providers.py \
  -k braintrust --junitxml=.mic/braintrust-live.xml
```

Absent opt-in means skipped. Opted-in tests with missing required fixture config
fail. JUnit properties capture provider provenance. The Braintrust test compares
two pinned reads. A fixture-owner-driven change between runs and an explicit
sink export remain separate acceptance checks; these read-only tests never
modify source data.

### SDK deadline behavior

The real-SDK transport tests exercise `Client.query`, `QueryJob.result` and
`RowIterator` through controlled HTTP responses. In google-cloud-bigquery 3.45.0,
`getQueryResults` requests can use an HTTP timeout of at least 120 seconds even
when the handle timeout is shorter. Mic waits for in-flight SDK calls to finish
before closing resources; its cooperative dataset deadline cannot force the SDK
thread to stop. The handle's timeout is therefore not a hard wall-clock cutoff.

## Custom sources

An inline factory can optionally accept a `mic.ReadContext` and yield records.
For reusable configuration, return a subclass of `mic.DatasetSource`. Its
`read(ctx)` returns a synchronous or asynchronous iterator of raw records. The
same interface powers every built-in source; sources do not register loaders or
import Mic runtime internals.

```python
from collections.abc import Iterator
from dataclasses import dataclass

import mic

@dataclass(frozen=True)
class RangeSource(mic.DatasetSource):
    stop: int

    def read(self, ctx: mic.ReadContext) -> Iterator[object]:
        ctx.set_provenance(provider="range", stop=self.stop)
        for value in range(self.stop):
            yield mic.RawCase(input=value, expected=value * 2)

@mic.dataset(input=int, expected=int)
def numbers() -> RangeSource:
    return RangeSource(100)
```

Keep construction passive. Create clients and open files inside `read`, with
`try/finally`, `with`, or `async with` owning their lifetime. Mic closes the
iterator on exhaustion or early exit; synchronous iteration and cleanup use the
same serialized worker thread. Async implementations must make their own blocking
SDK calls cancellation-safe. Exceptions terminate the source; a provider which
can recover at a record boundary may yield `mic.RecordError` with a safe message.
The consumer aborts by default; `on_invalid="skip"` skips it. Fatal iterator
exceptions and limits always fail.

`ReadContext.limits` exposes immutable `row_count` and `timeout_seconds`.
`rows_seen` counts raw records before validation; `remaining_rows` gives the
remaining selection. `remaining_seconds` decreases only during active read work.
These properties are read-only; remaining values are `None` when unlimited.
Providers may use them to size pages and bound each SDK request timeout.
Built-in BigQuery and Braintrust requests use the smaller of their configured
timeout and the remaining budget. BigQuery page sizing does not limit scan cost,
and Mic does not rewrite arbitrary SQL to add LIMIT. `set_provenance(**values)` validates
and copies finite JSON; cumulative provenance is limited to 64 KiB. The context
counts raw rows centrally, independently of provider implementation. Providers
open files and clients using ordinary Python APIs and own their cleanup; no
byte accounting or Mic-specific file wrapper is required. File-byte digests are
an implementation detail of the built-in file provider, not a source obligation.

See [the YAML example](../examples/yaml_source.py) for a multi-document source
with resource cleanup, duplicate-key/alias rejection, and bounded nesting. YAML
is an example dependency, not a core or provider extra. Each YAML document is
parsed in memory, and Mic validates each yielded record. It is not a
general-purpose sandbox for hostile YAML.
