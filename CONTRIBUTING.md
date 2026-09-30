# Development

Mic is a small, maintainer-led project in early development. Its scope is
intentionally limited. Please discuss substantial changes before investing in an
implementation. There is no guaranteed review or support turnaround.

## Set up

Use Python 3.12 or later, [uv](https://docs.astral.sh/uv/), and Node.js 18 or later
(CI uses Node 22). From a checkout:

```console
uv sync --frozen --all-extras
npm ci --ignore-scripts --prefix tests/reporting/js
```

Node is needed only for report-interaction tests, not to use Mic. Optional Python
integrations are installed here for testing; Mic's core has no runtime dependencies.

## Make and verify a change

Work on a branch. Run relevant tests while developing, for example:

```console
uv run pytest tests/packaging -q
```

Before submitting a change, run the canonical checks:

```console
uv run python scripts/verify.py
```

For packaging or release changes, also run:

```console
uv build --no-create-gitignore
uv run python scripts/verify_release.py artifacts
uv run twine check --strict dist/*
uv run python scripts/verify_packaging.py
```

Use a clean `dist/` directory for release checks: stale distributions and extra
files (including a `.gitignore` created by plain `uv build`) are rejected. Move
previous build output aside before rebuilding if necessary.

See [verification](docs/verification.md) for evidence, coverage boundaries, and
opt-in integration tests. Ordinary development requires no live provider credentials.

Keep changes focused, include tests for observable behavior and failure paths, and
update documentation when behavior changes. Keep credentials, private datasets, and
real provider responses out of fixtures, logs, and submitted artifacts. Preserve the
dependency-free core and lazy optional integrations.

The [engineering agreement](AGENTS.md) describes the repository layout, invariants,
and approval boundaries for API and dependency changes. The PR template asks for
the reason for a change, verification evidence, compatibility implications, and
remaining gaps. A maintainer performs the final merge.

For publication, see the [release checklist](docs/releasing.md).
