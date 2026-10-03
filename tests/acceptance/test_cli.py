"""Exercise the public CLI in isolated processes using the installed entry module."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def invoke(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "mic.cli", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize("command", ["inspect", "preflight", "run"])
def test_dataset_commands_expose_only_row_and_time_read_limits(command):
    result = invoke(command, "--help")
    assert result.returncode == 0
    assert "--max-rows" in result.stdout
    assert "--dataset-timeout" in result.stdout
    assert "--max-bytes" not in result.stdout
    assert "--max-record-bytes" not in result.stdout


def test_report_retains_its_independent_byte_cap():
    result = invoke("report", "--help")
    assert result.returncode == 0
    assert "--max-bytes" in result.stdout


def test_list_inspect_and_preflight() -> None:
    listing = invoke("list", "examples.triage", "--json")
    assert listing.returncode == 0, listing.stderr
    rows = json.loads(listing.stdout)
    assert {r["selector"] for r in rows} >= {
        "examples.triage:baseline",
        "examples.triage:fixed",
        "examples.triage:triage_data",
    }
    inspected = invoke("inspect", "examples.triage:triage_data", "--limit", "2")
    assert inspected.returncode == 0, inspected.stderr
    assert len(json.loads(inspected.stdout)["rows"]) == 2
    preflight = invoke("preflight", "examples.triage:baseline")
    assert preflight.returncode == 0, preflight.stderr
    assert json.loads(preflight.stdout)["dataset"]["records_accepted"] == 3


def test_baseline_fixed_gate_and_rerender(tmp_path: Path) -> None:
    baseline = invoke("run", "examples.triage:baseline", "--output", str(tmp_path / "baseline"))
    assert baseline.returncode == 0, baseline.stderr
    assert "exact: mean=0.666" in baseline.stdout
    fixed = invoke("run", "examples.triage:fixed", "--output", str(tmp_path / "fixed"), "--json")
    assert fixed.returncode == 0, fixed.stderr
    before = json.loads((tmp_path / "baseline" / "run.json").read_text(encoding="utf-8"))
    after = json.loads(fixed.stdout)
    assert next(iter(before["summary"]["tasks"].values()))["scores"]["exact"][
        "mean"
    ] == pytest.approx(2 / 3)
    assert next(iter(after["summary"]["tasks"].values()))["scores"]["exact"]["mean"] == 1.0
    assert (
        next(iter(before["sources"].values()))["digest"]
        == next(iter(after["sources"].values()))["digest"]
    )
    assert (
        before["summary"]["trials"]["task_failed"] == after["summary"]["trials"]["task_failed"] == 0
    )
    for name in ("run.json", "events.jsonl"):
        assert (tmp_path / "baseline" / name).is_file()
    gate = invoke(
        "run",
        "examples.triage:baseline",
        "--require",
        'tasks["triage.baseline"].scores["exact"].mean>=0.9',
        "--output",
        str(tmp_path / "gate"),
    )
    assert gate.returncode == 1, gate.stderr
    assert "Status       failed" in gate.stdout
    rendered = invoke(
        "report", str(tmp_path / "baseline"), "--output", str(tmp_path / "review.html")
    )
    assert rendered.returncode == 0, rendered.stderr
    assert (tmp_path / "review.html").is_file()


@pytest.mark.parametrize("selector", ["task_error", "output_schema", "scorer_error"])
def test_failure_artifacts_name_errors(selector: str, tmp_path: Path) -> None:
    process = invoke("run", f"examples.failures:{selector}", "--output", str(tmp_path / selector))
    assert process.returncode == 1, process.stderr
    manifest = json.loads((tmp_path / selector / "run.json").read_text(encoding="utf-8"))
    trials = manifest["summary"]["trials"]
    assert trials["task_failed"] + trials["scoring_failed"] > 0
    events = [
        json.loads(line) for line in (tmp_path / selector / "events.jsonl").read_text().splitlines()
    ]
    failures = [
        error for e in events if e["type"] == "trial_finished" for error in e["result"]["errors"]
    ]
    assert failures and all(e["phase"] and e["message"] for e in failures)


def test_nullable_scores_preserve_missing_vs_null(tmp_path: Path) -> None:
    process = invoke(
        "run", "examples.failures:null_score", "--output", str(tmp_path / "null"), "--json"
    )
    assert process.returncode == 0, process.stderr
    manifest = json.loads(process.stdout)
    stats = next(iter(manifest["summary"]["tasks"].values()))["scores"]["nullable_exact"]
    assert (
        stats["count"] == 1
        and manifest["summary"]["trials"]["scoring_skipped"] == 2
        and stats["mean"] == 1.0
    )
    cases = [
        event["result"]
        for line in (tmp_path / "null" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if (event := json.loads(line))["type"] == "trial_finished"
    ]
    cases_by_id = {case["label"]: case for case in cases}
    assert "expected" not in cases_by_id["missing"]
    assert cases_by_id["null"]["expected"] is None


def test_async_trials(tmp_path: Path) -> None:
    process = invoke(
        "run",
        "examples.async_eval:uppercase",
        "--trials",
        "2",
        "--concurrency",
        "2",
        "--output",
        str(tmp_path / "async"),
        "--json",
    )
    assert process.returncode == 0, process.stderr
    manifest = json.loads(process.stdout)
    assert manifest["summary"]["trials"]["completed"] == 6
    cases = [
        event["result"]
        for line in (tmp_path / "async" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if (event := json.loads(line))["type"] == "trial_finished"
    ]
    # The journal records completion order, which may differ across event loops.
    # Sorting for comparison still catches missing, duplicate, or unexpected trials.
    assert sorted((case["row_index"], case["trial"]) for case in cases) == [
        (row_index, trial) for row_index in range(3) for trial in (1, 2)
    ]
    for case in cases:
        assert case["label"] == f"async-{case['row_index']}"
        assert case["status"] == "completed"
        assert case["output"] == case["expected"] == f"ITEM {case['row_index']}"


@pytest.mark.parametrize(
    "args",
    [
        ("run", "examples.triage:base"),
        ("run", "examples.triage:exact"),
        ("inspect", "examples.triage:triage_data", "--max-rows", "1"),
        ("preflight", "examples.triage:baseline", "--trials", "0"),
        ("preflight", "examples.triage:baseline", "--braintrust-experiment", "unconfigured"),
    ],
)
def test_bad_configuration_has_exit_2(args: tuple[str, ...]) -> None:
    process = invoke(*args)
    assert process.returncode == 2, process.stderr
    assert "mic:" in process.stderr


def test_estimate_command_is_not_available() -> None:
    result = invoke("estimate", "examples.triage:triage_data")
    assert result.returncode == 2
    assert "invalid choice" in result.stderr
