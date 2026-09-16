"""Reproduce local proof and record exact commands and source identities."""

import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    evidence = root / ".artifacts" / "verification"
    logs = evidence / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    python = sys.executable
    environment = os.environ.copy()
    environment["COVERAGE_FILE"] = str(evidence / ".coverage")
    checks = [
        (
            "tests",
            [
                python,
                "-m",
                "coverage",
                "run",
                "--branch",
                "--source=mic",
                "-m",
                "pytest",
                "-q",
                "--junitxml=.artifacts/verification/tests.xml",
            ],
        ),
        (
            "coverage_json",
            [python, "-m", "coverage", "json", "-o", ".artifacts/verification/coverage.json"],
        ),
        ("coverage_report", [python, "-m", "coverage", "report", "--show-missing"]),
        ("strict_typing", [python, "-m", "pyright", "--pythonpath", python]),
        ("authoring_typing", [python, "scripts/verify_typing.py"]),
        ("lint", [python, "-m", "ruff", "check", "src", "tests", "examples", "scripts"]),
        (
            "format",
            [python, "-m", "ruff", "format", "--check", "src", "tests", "examples", "scripts"],
        ),
    ]
    node = shutil.which("node") or "node"
    checks.append(("report_javascript", [node, "--check", "src/mic/reporters/templates/report.js"]))
    checks.append(("report_interactions", [node, "--test", "tests/reporting/js/report.test.mjs"]))
    records = []
    for name, command in checks:
        start = time.monotonic()
        try:
            completed = subprocess.run(
                command, cwd=root, env=environment, text=True, capture_output=True, check=False
            )
            output = completed.stdout + completed.stderr
            exit_code = completed.returncode
        except OSError as exc:
            output = f"Cannot execute verification command: {exc}. Install Node >=18 for report interaction tests.\n"
            exit_code = 1
        (logs / f"{name}.txt").write_text(output, encoding="utf-8")
        records.append(
            {
                "name": name,
                "command": command,
                "exit_code": exit_code,
                "duration_seconds": round(time.monotonic() - start, 3),
                "log": f"logs/{name}.txt",
            }
        )
        print(f"{name}: {'PASS' if exit_code == 0 else 'FAIL'}", flush=True)
        if exit_code:
            print("\n".join(output.splitlines()[-20:]), flush=True)
    sources = {}
    for directory in ("src", "tests", "examples", "scripts"):
        for current, directories, files in os.walk(root / directory):
            directories[:] = sorted(set(directories) - {"__pycache__", "node_modules"})
            for filename in sorted(files):
                path = Path(current) / filename
                if path.suffix != ".pyc":
                    sources[str(path.relative_to(root))] = hashlib.sha256(
                        path.read_bytes()
                    ).hexdigest()
    for name in ("pyproject.toml", "uv.lock"):
        sources[name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
    test_counts = {}
    junit = evidence / "tests.xml"
    if junit.exists():
        suites = list(ET.parse(junit).iter("testsuite"))
        test_counts = {
            key: sum(int(suite.attrib.get(key, 0)) for suite in suites)
            for key in ("tests", "failures", "errors", "skipped")
        }
    versions = {
        name: importlib.metadata.version(name)
        for name in ("mic-evals", "pytest", "pyright", "ruff", "coverage")
    }
    for name in ("pydantic", "google-cloud-bigquery", "braintrust", "httpx"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "not installed"
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "python": sys.version,
        "checks": records,
        "test_counts": test_counts,
        "coverage": {
            "report": "coverage.json",
            "scope": "mic Python code exercised in the pytest process; subprocess CLI and Node DOM tests run separately",
        },
        "versions": versions,
        "source_sha256": sources,
        "live_cloud_verification": "not claimed by this script",
        "browser_visual_verification": "not performed; browser URL policy blocked navigation",
    }
    (evidence / "verification.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Evidence: {evidence / 'verification.json'}")
    return int(any(record["exit_code"] != 0 for record in records))


if __name__ == "__main__":
    raise SystemExit(main())
