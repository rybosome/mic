"""Require good authoring to pass and every marked mistake to fail statically.

Run from the project root with the development environment's Python interpreter.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/typing"
EXPECTED = {
    (str(path), index)
    for path in FIXTURES.glob("*.py")
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
    if "# expect-error" in line
}

completed = subprocess.run(
    [
        sys.executable,
        "-m",
        "pyright",
        "--project",
        str(FIXTURES / "pyrightconfig.json"),
        "--pythonpath",
        sys.executable,
        "--outputjson",
    ],
    cwd=ROOT,
    check=False,
    capture_output=True,
    text=True,
)
try:
    report = json.loads(completed.stdout)
except json.JSONDecodeError:
    print(completed.stdout, completed.stderr, file=sys.stderr)
    raise SystemExit("Pyright did not return a diagnostic report") from None
errors = [item for item in report["generalDiagnostics"] if item["severity"] == "error"]
actual = {(item["file"], item["range"]["start"]["line"] + 1) for item in errors}
unexpected = actual - EXPECTED
missing = EXPECTED - actual
if unexpected or missing or completed.returncode not in (0, 1):
    print(json.dumps(report, indent=2))
    if unexpected:
        print(f"Unexpected diagnostic locations: {sorted(unexpected)}", file=sys.stderr)
    if missing:
        print(f"Expected type errors were NOT reported: {sorted(missing)}", file=sys.stderr)
    raise SystemExit(1)
print(
    f"Typing verified: positive authoring accepted; all {len(EXPECTED)} deliberate mistakes rejected."
)
