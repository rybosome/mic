# Dataset sources

Dataset factories return passive handles or fresh Python iterables. The dataset
schema and optional `map_row` function define evaluation cases; a source does not
choose the task, output schema, scorer, or reporting destination. All adapters use
the same resolver and bounded materializer. Importing their modules does not
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

`ReadLimits` defaults to `max_rows=10_000`, `max_bytes=67_108_864`,
`max_record_bytes=1_048_576`, and `timeout_seconds=60`. Exceeding a safety cap fails
dataset loading before tasks run; it never selects an implicit prefix. `mic
inspect ... --limit N` and `inspect_dataset(..., limit=N)` deliberately select a
prefix. The source adapters check raw sizes where available, and the materializer
also checks the normalized, serialized case sizes.
`inspect_dataset` is blocking; async hosts use `await ainspect_dataset(...)`.

Rows normally contain `input`, optional `expected`, optional object-shaped
`metadata`, and optional string `id`. Missing `expected` is distinct from an
explicit JSON `null`. Dataset schemas require expected values unless configured
with `expected_policy="optional"`. Custom mappers return `RawCase` and can map
arbitrary source columns to that envelope. Decode source-specific JSON string
columns explicitly in that mapper so normalization remains visible in the definition.

Explicit IDs are preserved and duplicates fail loading. Default IDs for cases
without an explicit ID derive from normalized case content and position. Snapshot
digests include normalized inputs, expected values, metadata, and IDs, and exclude
provider provenance. Cross-provider equality therefore needs equal logical IDs
and ordering. Braintrust physical record IDs remain in per-case provenance even
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
from mic.providers.files import FileHandle

FileHandle(Path("fixtures/triage.jsonl"))
FileHandle("fixtures/cases.json", format="json")
```

`FileHandle(path, format=None)` accepts `Path` or string. `.jsonl` and `.ndjson`
extensions select JSONL; other paths default to a JSON array. An explicit format
is either `"json"` or `"jsonl"`. JSONL is read one bounded line at a time. A JSON
array is read into a buffer bounded by `max_bytes` before parsing.

Blank JSONL lines are skipped. Malformed JSON, duplicate object keys, nonfinite
numbers, non-object rows, and invalid source provenance fail with file/line
context. Custom columns are retained for mappers. Physical file path, row number,
and JSONL line number are added to each source row's `provenance`.

A complete file read records `raw_sha256`, `raw_bytes`, and
`read_complete=true`. A selected JSONL prefix records `raw_prefix_sha256` and
`read_complete=false`, so it cannot be mistaken for a complete-file digest.

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

The loader performs a dry run with Standard SQL and query cache disabled, checks
the server's statement type is exactly `SELECT`, and rejects estimated bytes over
the cap before submitting the executable query. Scripts, DML, and DDL fail this
check. The execution also carries `maximum_bytes_billed`. SQL identifiers belong
in explicit SQL configuration; use parameters for values. A `LIMIT` clause is a
row selection and is not a BigQuery scan-cost guarantee. Supply a deterministic,
unique ordering key; the adapter does not attempt to prove SQL ordering.

Rows remain raw column mappings, including SDK-native datetime/Decimal/bytes
values, for your mapper. Provenance includes SQL, named parameter values, billing
project/location, a stable query identity hash, dry-run estimate, job ID, and
available actual processing/billing/cache metadata. Query values are evidence;
do not put credentials in SQL parameters.

`await BigQueryLoader().estimate(handle)` performs only the dry run. Inject a
`client` to retain caller ownership, or a `client_factory` to transfer client
ownership to the loader. `config_factory(handle, dry_run)` supports SDK-free fake
client tests. Owned clients close on success, error, and cancellation; query
cancellation is best effort.

References: [QueryJobConfig](https://docs.cloud.google.com/python/docs/reference/bigquery/latest/google.cloud.bigquery.job.QueryJobConfig),
[query parameters](https://docs.cloud.google.com/bigquery/docs/parameterized-queries),
[Application Default Credentials](https://cloud.google.com/docs/authentication/application-default-credentials).

## Braintrust

Install the `braintrust` extra. The source uses the pinned Braintrust SDK's owned
HTTP session and the read-only BTQL endpoint; it never calls `init_dataset`,
creates a dataset, or writes experiments.
Experiment export is the independent `BraintrustReporter` integration.

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

Set `BRAINTRUST_API_KEY`, or supply `BraintrustLoader(api_key=...)`. Read settings
only when opening the dataset. The default source client is owned and closed by
the loader. Injected clients implement the small async `ReadClient` protocol and
remain caller-owned; an existing `httpx.AsyncClient` satisfies that protocol.
The `transport=` convenience parameter still supports explicit HTTPX mock
transports when HTTPX is separately installed, and creates a loader-owned client.
Production defaults never import HTTPX. The API key does not enter dataset
provenance and is passed explicitly without a fallback to unrelated `.netrc`
credentials.

The pinned SDK's high-level dataset fetcher buffers all pages, and its legacy
long-lived HTTP adapter can eagerly read entire response bodies. To retain read
caps, the adapter uses the ordinary session owned by `BraintrustClient` with
`stream=True`, redirects disabled, and an explicit request timeout. Blocking
request/read operations run in worker threads. Cancellation joins the in-flight
read before closing its response and client. This low-level SDK bridge is covered
by tests against 0.39.0 and is a compatibility point to recheck on SDK upgrades.

Each `/btql` request has a structured query selecting existing dataset rows in
`_pagination_key` order, a bounded page limit, `fmt="jsonl"`, and the pinned
top-level `version`. The reader streams response bytes and reads cursors from
`x-bt-cursor` or `x-amz-meta-bt_cursor`. The documented cursor flow ends at an
empty page without a cursor. A nonempty page missing its cursor, a repeated
cursor, an empty page with a cursor, or a response exceeding its requested page
limit fails explicitly. The adapter probes for an extra row when the configured
row cap is reached, distinguishing a complete dataset from silent truncation.

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
reporter export remain separate acceptance checks; these read-only tests never
modify source data.

### SDK deadline behavior

The real-SDK transport tests exercise `Client.query`, `QueryJob.result` and
`RowIterator` through controlled HTTP responses. In google-cloud-bigquery 3.45.0,
`getQueryResults` requests can use an HTTP timeout of at least 120 seconds even
when the handle timeout is shorter. Mic waits for in-flight SDK calls to finish
before closing resources; its cooperative dataset deadline cannot force the SDK
thread to stop. The handle's timeout is therefore not a hard wall-clock cutoff.
