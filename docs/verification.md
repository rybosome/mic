# Verification and human acceptance

Run the package checks from the project directory:

```console
uv sync --frozen --all-extras
npm ci --ignore-scripts --prefix tests/reporting/js
uv run python scripts/verify.py
```

The script records exact commands, exit codes, tool versions, source hashes, logs,
JUnit results, and Python line/branch coverage under the ignored
`.artifacts/verification/` directory. Node 18+ and the locked jsdom development
package are needed for report interaction tests; neither is shipped in the Python
wheel or required to open a report.

## Continuous integration

GitHub Actions runs three independent gates for pull requests, pushes to `main`,
and manual dispatches:

1. **Full verification** runs the canonical verifier on Python 3.12 with every
   optional dependency and the locked report-test dependencies.
2. **Compatibility** runs the Python test suite on Python 3.12 across Linux,
   macOS, and Windows, and on supported newer Python versions on Linux.
3. **Packaging** builds the wheel and source distribution, then runs the clean,
   dependency-free installed-wheel acceptance test.

The full and packaging jobs upload their generated evidence for 14 days, including
on failure. CI never opts in to live provider tests, discovers cloud fixtures, or
performs remote writes. Authenticated provider checks remain explicit manual gates.

Test scopes:

| Scope | What it proves |
| --- | --- |
| Runtime | Bounded read-ahead/live records, shared-source suites, global concurrency, isolation, cancellation, record policies, requirements, optional evidence |
| Schema and datasets | Dataclass inheritance, nested collections, constructors/defaults, optional Pydantic, typed hydration, source identity and limits |
| Providers | SDK pagination, native parameters/rows, failure and cancellation cleanup, source equivalence |
| Sinks and CLI | Installed entrypoint, lazy listing, HTML fidelity and atomic writes, escaping/CSP, SDK upload failures and mutation isolation |
| Report DOM behavior | Search/filters, keyboard focus and tabs, empty results, null/missing/zero, notices and clipboard fallback |
| Typing | Valid public authoring accepted; deliberate API mistakes rejected |
| Live integrations | Provider behavior only when explicit fixture configuration is supplied |

The normal suite does not access production data. Installed Braintrust and BigQuery
SDKs execute request, pagination, and decoding code against controlled local responses.
Smaller client doubles cover fault injection and cleanup. These checks are distinct
from live integration tests; see [providers](providers.md) for the two opt-in commands.
Live Braintrust experiment export remains a separate manual acceptance check.

Artifact tests inject permission, disk-full, replacement, rendering, and cleanup
failures through the real runner. They check retained summaries/events, bounded publication recovery,
cooperative cancellation, failed-sink isolation, and no task/write replay.
Atomic-write tests check that partial writes do not replace earlier files. These
controlled failures do not establish power-loss durability or filesystem-wide
transactionality; real filesystem behavior still depends on the operating system
and storage. No live provider access is needed for these tests.

Coverage is a guide to untested behavior rather than a release claim. CLI subprocesses,
clean-wheel smoke runs, and Node DOM tests have separate pass/fail results and are not
fully attributed to the main Python coverage process.

### Artifact conformance and coverage gates

The [artifact schemas](artifacts.md) are checked against real completed, failed,
cancelled, and partial-run records. Negative tests mutate fields to prove that
invalid versions, statuses, coordinates, types, and required-field omissions are
rejected. References resolve locally; format validation and finite-JSON checks are
explicit. `jsonschema`, `referencing`, and the date-time checker are development-only
dependencies, never core runtime or reader dependencies.

The canonical verifier enforces **93% statement coverage** and **84% branch coverage**
of `mic` in the main pytest process. `scripts/check_coverage.py` compares raw covered
and total counts separately, without rounding. The combined percentage displayed
by coverage.py is not the statement percentage. Measurements, thresholds, and outcomes
are recorded in `coverage-gates.json`; missing/malformed evidence fails verification.
Previous coverage evidence is cleared before collection to prevent stale passes.
No measured statements is an error; a measured program with no branches has no
branch obligations. The floors guard regressions, not complete behavioral coverage.

Focused checks:

```console
uv run pytest -q tests/runtime/test_artifact_contract.py tests/verification/test_coverage.py
```

## Dataclass and dependency walkthrough

Run `uv run --no-dev mic inspect examples.structured:tickets --limit 2`, then run
both `examples.structured:classify` and `examples.structured:classify_file` with
`--trials 3`. The datasets should have the same logical digest, six completed executions, and means
of 1.0 for both metrics.

Run the optional example with:

```console
uv run --extra pydantic mic run examples.pydantic_models:double
```

The input alias `quantity` becomes canonical `count`, while `Field(ge=0)` remains
enforced. The [schema guide](schemas.md) describes the complete boundary behavior.

## Streaming integration checks

`tests/acceptance/test_streaming_integrations.py` runs BigQuery, Braintrust, and
the external-style YAML provider through shared-source suite execution, a fake
Braintrust export destination, current artifact validation, and offline rendering.
Controlled BigQuery/Braintrust pagination waits for a scorer to run before returning
the next page, proving the runtime does not materialize the dataset first.

Malformed records at known boundaries exercise both abort and skip. Separate
provider tests retain fatal transport/size/pagination checks. YAML parser framing
failures remain fatal; a malformed but parsed case can be skipped.

Live-record and slow-sink tests prove bounded framework retention/read-ahead.
They do not establish an exact RSS bound for arbitrary user models, SDK pages,
custom sinks, or hostile parsers. Streaming runs and summaries are bounded;
explicit HTML rendering intentionally materializes within its configured caps.

## Human report walkthrough

Generate reports using the README commands, then check:

1. The baseline completes three executions and reports an exact mean of 0.667.
2. The fixed evaluation reports 1.0 against the same dataset digest.
3. The nullable example distinguishes an unscored value from zero and failure.
4. The failure example preserves successful trial events and shows the failed phase
   and exception type without raw exception bodies or tracebacks.
5. `--require 'tasks["triage.baseline"].scores["exact"].mean>=0.9'` exits 1 for the baseline without converting low quality
   into an execution error.

The HTML provides case search, execution/unscored filtering, keyboard navigation,
copyable IDs, raw JSON, and provenance. jsdom verifies behavior but not visual
layout, native clipboard permissions, printing, or CSP enforcement in a real browser.

## Reproducing packaged installs

```console
uv build
uv run python scripts/verify_packaging.py
```

The packaging verifier checks wheel membership and contents, MIT license metadata,
the source archive, and a clean installation with `mic-evals` as the only installed
distribution. It then exercises list, inspect, preflight, run, and report using both
memory and file datasets. Its generated record is written under `.artifacts/packaging/`.
