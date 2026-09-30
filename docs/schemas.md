# Dataclass-first schema support

Input, expected, output and metadata can all be complex objects. Standard-library
dataclasses are the primary API, and the mic core has no third-party runtime
dependencies. A provider supplies data; it does not choose your domain model.

The distinction matters: Python dataclasses generate constructors and related
methods, but generally do not check field annotations. Mic validates them at
dataset and task-output boundaries. [Python dataclasses documentation](https://docs.python.org/3.12/library/dataclasses.html)

## Author ordinary Python classes

```python
from dataclasses import dataclass, field
from typing import Literal

@dataclass(frozen=True, slots=True)
class Message:
    role: Literal["user", "assistant"]
    text: str

@dataclass
class Ticket:
    messages: list[Message]
    history: dict[str, list[Message]] = field(default_factory=dict)

@dataclass
class Decision:
    label: Literal["bug", "feature", "question"]
    evidence: dict[str, list[int]] = field(default_factory=dict)
    owner: str | None = None
```

No mic base class, special field annotation, or model library is needed. In the
same module, bind your types to a dataset and evaluation:

```python
import mic

@mic.dataset(name="tickets", schema=mic.case_schema(input=Ticket, expected=Decision))
def tickets() -> list[mic.RawCase]:
    return [mic.RawCase(
        id="crash",
        input=Ticket([Message("user", "It crashes on startup")]),
        expected=Decision("bug", {"messages": [0]}),
    )]

@mic.scorer(name="exact")
def exact(ctx: mic.ScoreContext[Ticket, Decision, Decision, mic.JsonObject]) -> float:
    return float(ctx.output == ctx.require_expected())

@mic.eval(name="classify", dataset=tickets, output=Decision, scorers=[exact])
def classify(ctx: mic.TaskContext[Decision, mic.JsonObject], ticket: Ticket) -> Decision:
    for index, message in enumerate(ticket.messages):
        if "crash" in message.text.lower():
            return Decision("bug", {"messages": [index]})
    return Decision("question")
```

TaskContext's first type parameter is the expected type. ScoreContext's parameters
are input, output, expected, metadata, in that order. `require_expected()` returns
the typed expected object. Tasks and scorers can both be async.

The complete runnable [structured example](../examples/structured.py) additionally
demonstrates typed metadata, task metadata merging, two metrics, and equivalent
native-memory and JSONL data. [Its fixture](../examples/fixtures/structured.jsonl)
uses ordinary JSON:

```json
{"id":"crash","input":{"messages":[{"role":"user","text":"It crashes on startup"}]},"expected":{"label":"bug","evidence":{"messages":[0]}}}
```

Mic hydrates `Ticket`, its nested `Message` instances, and `Decision` automatically.
Omitted fields with defaults are filled. The task receives objects and uses normal
attribute access. JSON evidence stores their field projection, including defaults
and explicit nulls. You can return a native dataclass or a JSON-compatible object
matching the declared output type; runtime validation hydrates either. Annotating a
task as returning `Decision` and returning a `Decision` retains static checking too.

## Swap providers without changing schemas or scoring

```python
from pathlib import Path
from mic.providers.files import FileHandle
from mic.providers.bigquery import BigQueryHandle
from mic.providers.braintrust import BraintrustHandle

case_types = mic.case_schema(input=Ticket, expected=Decision)

@mic.dataset(name="tickets.file", schema=case_types)
def file_tickets() -> FileHandle:
    return FileHandle(Path(__file__).with_name("tickets.jsonl"))

@mic.dataset(name="tickets.bigquery", schema=case_types)
def bigquery_tickets() -> BigQueryHandle:
    return BigQueryHandle(
        billing_project="your-project", location="US",
        maximum_bytes_billed=100_000_000,
        sql="SELECT id, input, expected FROM `your-project.evals.tickets` ORDER BY id",
    )

@mic.dataset(name="tickets.braintrust", schema=case_types)
def braintrust_tickets() -> BraintrustHandle:
    return BraintrustHandle(dataset_id="existing-id", xact_id="pinned-xact-id")
```

BigQuery input/expected columns can be nested records and arrays matching the JSON
shape. For different source columns, supply `map_row=...` returning `mic.RawCase`;
decode source-specific JSON string columns explicitly in that mapper.
Use the desired factory in `@mic.eval(dataset=...)`. Each handle remains passive
until selected; see [provider contracts](providers.md) for actual configuration.

## Native support and strictness

| Definition | Behavior |
| --- | --- |
| Nested dataclasses | Rebuilt typed objects from mappings or native instances |
| `list[T]`, `dict[str, T]`, `Mapping[str, T]` | Recursively checked; maps hydrate to dictionaries |
| `Sequence[T]`, fixed/variable tuples | Sequences hydrate to lists; tuples hydrate to tuples and persist as arrays |
| `str`, `bool`, `int`, `float`, `None` | Exact primitive kinds; integer-to-float numeric widening is allowed |
| `T \| None`, unions, `Literal`, Enum with JSON primitive values | Nullable/variant validation; enum values persist as JSON primitives |
| Defaults and `default_factory` | Omitted fields receive independently validated default values |
| Frozen, slots, keyword-only fields, inheritance | Supported without changing the authoring syntax |
| Standard-library TypedDict | Required/NotRequired fields checked; values remain dictionaries |
| Forward/recursive references and PEP 695 aliases | Supported for resolvable definitions and finite values; forward-referenced types should be module-level |
| `Any`/`object` | Finite JSON-shaped values; this does not serialize arbitrary Python objects |

Unknown dataclass fields, missing required fields, nonfinite numbers, value cycles,
non-string map keys, and nested type mismatches fail with a field/index path. For
example, a number in `Message.text` reports `$.messages[0].text`. Dataset errors add
row/source context and launch zero tasks. An invalid output becomes a schema-phase
case failure before scoring.

Native schemas do not coerce strings into numbers, and reject `strict=False`
explicitly. Normalize source data in a mapper. Union branches are tried in annotation
order; use a distinguishing `Literal` field for structurally similar dataclasses.
Snapshots must preserve a meaningful round trip; validation is not a general object
serialization mechanism.

`InitVar`, `init=False` fields, undeclared instance state, parameterized generic
dataclasses/type aliases, arbitrary classes, bytes, datetime/Decimal, sets, and
`Annotated` constraint metadata need explicit mapping or a schema adapter. These
are rejected instead of silently losing fields or ignoring constraints.

Mapping hydration invokes the dataclass constructor and `__post_init__`, then checks
the resulting fields. Native-instance validation checks and clones fields without
rerunning constructor/normalization hooks. Thus mutated field types cannot bypass
validation, while repeated serialization does not repeatedly normalize an object.
It does not rerun arbitrary domain invariants in an existing instance's `__post_init__`.
Use an explicit adapter for invariants that must hold on every validation. Constructors,
default factories and validators should be pure. Frozen classes still need nested
copying; mic isolates every trial and scorer.

Optional expected is separate from nullable expected: `expected_policy="optional"`
allows the field to be absent; `expected=Decision | None` allows an explicit null.
Supplied metadata can also be a dataclass but must serialize to an object. Task
metadata keys shallowly override the dataset metadata projection and the result is
rehydrated/validated before scoring. Dataset and task metadata remain separate in
the artifacts so the overwrite is inspectable.

## Optional Pydantic

Use it when aliases, constrained fields, custom validators, or discriminators are
worth the dependency. The core never needs it for ordinary dataclasses.

```python
from pydantic import BaseModel, Field
from mic.integrations.pydantic import pydantic_schema

class Request(BaseModel):
    count: int = Field(ge=0, alias="quantity")

request_schema = mic.case_schema(input=pydantic_schema(Request), expected=int)
```

Run the complete [Pydantic example](../examples/pydantic_models.py):

```console
uv run --no-dev --extra pydantic mic run examples.pydantic_models:double
```

Passing an existing BaseModel class directly also adapts it lazily for compatibility.
Explicit adapters make the dependency visible. Wrap a `TypeAdapter(...)` in
`pydantic_schema(...)` for complex constrained annotations. A stdlib dataclass
containing Pydantic `Field`/`Annotated` constraints must opt into that adapter;
native validation will not silently ignore the constraints. The optional adapter
rehydrates existing instances to enforce constraints, accepts aliases, and persists
canonical Python field names. [Pydantic adapter documentation](https://pydantic.dev/docs/validation/latest/concepts/type_adapter/)

## Small extension boundary

The runner uses this protocol, which has no dependency on a model library:

```python
from typing import Protocol

class Schema[T](Protocol):
    def validate(self, value: object, *, strict: bool = True) -> T: ...
    def dump(self, value: T) -> object: ...  # JSON-compatible data
    def json_schema(self) -> dict[str, object]: ...
```

`mic.schema(Ticket)` compiles this interface for a native type. You can use it
directly to hydrate values or inspect the schema, or compose it in a custom adapter.
Adapters must return independent validated values, emit finite JSON-compatible data,
and describe their actual contract accurately. The framework checks serialized
data and isolates consumers. [Custom constraint test](../tests/runtime/test_structured.py)

Type checking retains exact types for unions, lists and maps using `TypeForm` under
`TYPE_CHECKING`. `typing_extensions` is a development dependency, never a runtime
import or core dependency. [TypeForm documentation](https://typing-extensions.readthedocs.io/en/latest/#typing_extensions.TypeForm)

## CLI and verification

From the project directory, these run without development or optional packages:

```console
uv run --no-dev mic list examples.structured
uv run --no-dev mic inspect examples.structured:tickets --limit 2
uv run --no-dev mic preflight examples.structured:classify
uv run --no-dev mic run examples.structured:classify --trials 3 --output .mic/structured-memory
uv run --no-dev mic run examples.structured:classify_file --trials 3 --output .mic/structured-file
uv run --no-dev mic report .mic/structured-file --open
uv run --no-dev mic run examples.structured:classify --require 'label>=0.9'
```

Use fresh output directories or omit `--output` to get unique paths. Both providers
should show the same digest, six completed executions, and means of 1.0 for label and
evidence. Direct Python uses `mic.run(classify)` or `await mic.arun(classify)`.

The dependency contract is enforced by import-guard/no-site-packages tests and a
clean installed-wheel acceptance check. Package extras declare only `pydantic`,
`google-cloud-bigquery`, or `braintrust`, respectively; the SDKs retain their own
transitive dependencies. Braintrust streaming uses an SDK-owned session, so mic
no longer declares HTTPX as a provider dependency.

For all development checks: `uv sync --all-extras --frozen`, then `uv run python
scripts/verify.py`. For the independent clean installation: `uv build`, then
`uv run python scripts/verify_packaging.py`. Generated verification output lives
under the ignored `.artifacts/` directory.
