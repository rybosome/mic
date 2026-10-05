# Installation and environment

Use Python 3.12+ and the application's existing environment/package manager.
Install Mic alongside the code and SDKs it must import. A separate global tool
installation (including pipx or uv's tool interface) does not automatically see
project dependencies.

## uv project

From the project directory:

```sh
uv add mic-evals
source .venv/bin/activate
mic --help
```

For Windows PowerShell, use `.venv\Scripts\Activate.ps1`. Without activation,
use `uv run mic ...`. For a genuinely empty scratch directory, first run
`uv init --bare --python 3.12`; do not reinitialize an existing application.
If the project keeps evaluation tools in a dependency group, follow that convention.

For a pip-managed project, install with `python -m pip install mic-evals` inside
its activated environment. The core has no third-party runtime dependencies.

## Example and optional dependencies

The [complete support-ticket example](https://github.com/rybosome/mic/blob/main/examples/ticket_eval.py)
uses TypeSafe's Jev model. Its project needs:

```sh
uv add mic-evals typesafe-sdk
```

Set `TYPESAFE_API_KEY` through the user's credential mechanism. When given a key
file, load its contents directly into the child process environment without
printing them or placing the key in command arguments. Mic does not load `.env`
files implicitly. Running this example sends tickets to TypeSafe and incurs charges.
The SDK belongs to the example/application, not Mic's core.

Install extras only for selected features: `mic-evals[pydantic]`,
`mic-evals[bigquery]`, or `mic-evals[braintrust]`. Cloud datasets and Braintrust
export need their own configuration and authorized access. Read the corresponding
[provider](https://github.com/rybosome/mic/blob/main/docs/providers.md) or
[reporting](https://github.com/rybosome/mic/blob/main/docs/reporting.md) contracts
before configuring them; installing an extra alone does not configure access.

## Diagnose environment problems

Run these with the same environment used for Mic:

```sh
python -c 'import sys, mic; from importlib.metadata import version; print(sys.executable); print(mic.__file__); print(version("mic-evals"))'
mic list ticket_eval
```

- `mic` not found: activate the environment or use `uv run mic`.
- Evaluation module not found: run from its importable project root; selectors
  use Python module paths (`evals.tickets:classify`), not filesystem paths.
- SDK/application dependency missing: install it in this environment. A successful
  preflight cannot detect a missing dependency imported only inside the task.
- Version mismatch: inspect the selected executable and interpreter before
  changing dependencies. Do not silently upgrade an existing project's lockfile.

Keep the lockfile and evaluation code with meaningful experiments. Record the
model version/configuration and dataset identity separately when Mic's evidence
cannot capture them; a moving model alias is not a reproducibility guarantee.
