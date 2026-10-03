# Working on Mic

This file is the repository-wide working agreement for coding agents. Prefer these
principles over a long list of mechanical rules, and use judgment when a choice does not
cross one of the explicit human-approval boundaries below.

## Purpose and ownership

Mic provides typed, provider-independent micro-evaluations with bounded execution and
trustworthy, locally inspectable evidence. Correctness, reproducibility, explicit side
effects, and a dependency-free core take priority over feature breadth or convenience.

Humans own product purpose, audience, supported features, overall taste, and final merges
to `main`. Agents should offer product and API advice when useful, and otherwise own the
implementation: repository layout, module boundaries, naming, refactoring, tests,
documentation, and other engineering details. Do not ask for approval on routine,
reversible implementation choices.

Get explicit human approval before:

- making a breaking change to the public API;
- adding a core runtime dependency or an optional integration/SDK dependency;
- expanding the requested work into a new product capability; or
- reading from a live cloud service or performing a remote write unless the task already
  clearly authorizes that effect.

Development-only dependencies may be added when they are justified, narrowly scoped, and
do not leak into the shipped package. Agents may update this file through the normal pull
request process without separate advance approval.

## Engineering invariants

Preserve these properties unless an approved design explicitly changes them:

- The core package has no third-party runtime dependencies. Optional integrations remain
  optional and are imported lazily.
- Provider handles are passive descriptions. Import, discovery, listing, and inspection do
  not authenticate, contact remote services, or mutate state.
- Datasets stream through bounded queues without retaining the full dataset or result set.
  Factories return fresh sources; limits fail visibly rather than silently truncating data.
- Missing values and explicit `null` values remain distinct. JSON evidence is finite and
  does not silently coerce unsupported values.
- A failed trial or scorer does not erase unrelated successful work. Concurrency and
  cancellation behavior remain bounded and accurately described.
- Evidence recording and remote sinks are explicit opt-ins. Selected local recording runs
  first in event delivery order. Sinks receive isolated values and apply backpressure.
- Resource ownership and cleanup are explicit, including on failure and cancellation.
  Remember that synchronous work already running in a thread cannot be forcibly killed.
- Credentials, private response bodies, and other secrets do not appear in errors,
  artifacts, fixtures, logs, or tests.
- Keep dynamic SDK data at narrow integration boundaries; retain strict typing and explicit
  validation in the core.

Prefer standard-library dataclasses for core models, behavior-oriented tests, and comments
that explain invariants or non-obvious reasoning rather than restating code.

## Repository map

- `src/mic/`: public API, datasets, discovery, schema handling, execution, providers, and
  reporters. Internal runtime implementation belongs under `src/mic/_runtime/`.
- `tests/`: contract tests grouped by subsystem, plus acceptance, packaging, typing, and
  opt-in live integration coverage.
- `docs/`: detailed observable contracts and limitations. The README is the concise user
  entry point.
- `examples/`: executable examples and fixtures; keep them aligned with the public API.
- `scripts/verify.py`: canonical local verification and evidence capture.
- `scripts/verify_packaging.py`: built-artifact and clean-install verification.

Before introducing a new pattern, inspect the neighboring implementation and tests. Favor
the smallest coherent design that preserves the invariants above. Keep unrelated user
changes intact and avoid speculative abstractions or compatibility machinery.

## APIs, versions, and documentation

Treat exported names, documented call patterns, CLI behavior, provider contracts, artifact
formats, and persisted schemas as user-facing interfaces. Breaking public API changes need
explicit human approval as part of design, even while the project is in alpha.

Versioned formats are forward-looking only. When an approved breaking format change is
needed, increment the version and implement the new shape directly. Do not add backward
readers, migration layers, dual behavior, or documentation that teaches obsolete formats.

Update documentation and executable examples in the same pull request as user-visible
behavior. Describe what users can observe, including limits, side effects, failure modes,
and cancellation semantics. When release notes are maintained, identify a breaking change
without preserving obsolete implementation detail.

## Testing and verification

Verification is a reasoned confidence argument, not a command transcript or a test count.
For every pull request, identify the behavior and invariants at risk, exercise them at the
right layers, and explain why the evidence supports the change. State anything that was not
verified.

Run focused tests while developing and all relevant local checks before opening a pull
request. At minimum, do not knowingly leave basic unit, lint, formatting, or type failures
for CI to discover. Use the full verification path when the change is broad, touches core
contracts, or is otherwise high risk:

```console
uv sync --frozen --all-extras
npm ci --ignore-scripts --prefix tests/reporting/js
uv run python scripts/verify.py
```

For packaging, dependency, entry-point, or release-layout changes, also run:

```console
uv build
uv run python scripts/verify_packaging.py
```

Tests should prove observable behavior and failure paths, not implementation trivia. Prefer
controlled transports and local fakes for provider coverage. Live provider tests require
explicit opt-in and task authorization; never assume credentials or production access.
See `docs/verification.md` and `docs/providers.md` for current scopes and commands.

## Git and pull requests

Never develop directly on `main`. Work on a branch in the acting agent's namespace, make
focused commits, push the branch, and open a pull request. Agents may perform all normal Git
and pull-request operations, but a human performs the final merge to `main`.

Use stacked pull requests when a larger effort has clean reviewable dependencies. Agents
may decide when stacking improves review. Base each dependent pull request on its immediate
predecessor and make the relationship clear. Preserve published history unless rewriting it
is genuinely necessary and safe.

Every pull request should contain:

- **Executive summary:** what changed and the outcome;
- **Why:** the problem, user need, or engineering reason;
- **Design and implementation:** the important choices and how the solution works;
- **Verification and confidence:** risks exercised, evidence, results, and why that evidence
  is sufficient;
- **Compatibility and risks:** public API, format, dependency, side-effect, and operational
  implications;
- **Relationships and tracking:** stacked-PR relationships and issue links when relevant;
  and
- **Unverified or follow-up work:** explicit gaps, or `None`.

Keep pull requests scoped enough to review confidently. A complete change includes the
implementation, appropriate tests, documentation, examples, and cleanup it requires.

## Review priorities

Review for correctness and data integrity first, then explicit side effects and secret
safety, public contracts, cancellation and resource ownership, typing and test strength,
and finally maintainability. Treat automated formatting as tooling's job. Call out concrete
behavioral risk and evidence; avoid style-only review comments that do not improve the
project.
