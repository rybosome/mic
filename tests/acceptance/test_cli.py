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
    assert json.loads(preflight.stdout)["dataset"]["rows"] == 3


def test_baseline_fixed_gate_and_rerender(tmp_path: Path) -> None:
    baseline = invoke("run", "examples.triage:baseline", "--output", str(tmp_path / "baseline"))
    assert baseline.returncode == 0, baseline.stderr
    assert "exact 0.667" in baseline.stdout
    fixed = invoke("run", "examples.triage:fixed", "--output", str(tmp_path / "fixed"), "--json")
    assert fixed.returncode == 0, fixed.stderr
    before = json.loads((tmp_path / "baseline" / "run.json").read_text())
    after = json.loads(fixed.stdout)
    assert before["scores"]["exact"]["mean"] == pytest.approx(2 / 3)
    assert after["scores"]["exact"]["mean"] == 1.0
    assert before["dataset"]["digest"] == after["dataset"]["digest"]
    assert before["counts"]["failed"] == after["counts"]["failed"] == 0
    for name in ("run.json", "dataset.jsonl", "cases.jsonl", "report.html"):
        assert (tmp_path / "baseline" / name).is_file()
    gate = invoke(
        "run",
        "examples.triage:baseline",
        "--require",
        "exact>=0.9",
        "--output",
        str(tmp_path / "gate"),
    )
    assert gate.returncode == 1, gate.stderr
    assert "Quality      FAIL" in gate.stdout
    rendered = invoke(
        "report", str(tmp_path / "baseline"), "--output", str(tmp_path / "review.html")
    )
    assert rendered.returncode == 0, rendered.stderr
    assert (tmp_path / "review.html").is_file()


@pytest.mark.parametrize("selector", ["task_error", "output_schema", "scorer_error"])
def test_failure_artifacts_name_errors(selector: str, tmp_path: Path) -> None:
    process = invoke("run", f"examples.failures:{selector}", "--output", str(tmp_path / selector))
    assert process.returncode == 1, process.stderr
    manifest = json.loads((tmp_path / selector / "run.json").read_text())
    assert manifest["counts"]["failed"] > 0
    assert manifest["failures"]
    assert all(failure["phase"] and failure["message"] for failure in manifest["failures"])
    assert (tmp_path / selector / "report.html").is_file()


def test_nullable_scores_preserve_missing_vs_null(tmp_path: Path) -> None:
    process = invoke(
        "run", "examples.failures:null_score", "--output", str(tmp_path / "null"), "--json"
    )
    assert process.returncode == 0, process.stderr
    manifest = json.loads(process.stdout)
    stats = manifest["scores"]["nullable_exact"]
    assert stats["count"] == 1 and stats["null_count"] == 2 and stats["mean"] == 1.0
    cases = [
        json.loads(line) for line in (tmp_path / "null" / "cases.jsonl").read_text().splitlines()
    ]
    cases_by_id = {case["case_id"]: case for case in cases}
    assert "expected" not in cases_by_id["missing"]
    assert cases_by_id["null"]["expected"] is None


def test_async_trials_model_preset(tmp_path: Path) -> None:
    process = invoke(
        "run",
        "examples.async_eval:uppercase",
        "--trials",
        "2",
        "--concurrency",
        "2",
        "--model-preset",
        "offline-test",
        "--output",
        str(tmp_path / "async"),
        "--json",
    )
    assert process.returncode == 0, process.stderr
    manifest = json.loads(process.stdout)
    assert manifest["counts"]["completed"] == 6
    cases = [
        json.loads(line) for line in (tmp_path / "async" / "cases.jsonl").read_text().splitlines()
    ]
    assert all(case["task_metadata"]["model_preset"] == "offline-test" for case in cases)
    assert [(case["row_index"], case["trial"]) for case in cases] == sorted(
        (case["row_index"], case["trial"]) for case in cases
    )


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


@pytest.mark.parametrize("symbol", ["data", "evaluation"])
def test_estimate_invokes_factory_once_without_rows_or_tasks(
    symbol: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from types import ModuleType

    import mic
    import mic.cli as cli

    calls = {"factory": 0, "estimate": 0, "task": 0}
    handle = object()

    @mic.dataset(name="estimated.data", schema=mic.case_schema(input=str, expected=str))
    def data() -> object:
        calls["factory"] += 1
        return handle

    @mic.eval(name="estimated.eval", dataset=data, output=str, scorers=[])
    def evaluation(context: mic.TaskContext[str, mic.JsonObject], value: str) -> str:
        calls["task"] += 1
        raise AssertionError("estimate must not execute tasks")

    class FakeResolver:
        async def estimate(self, source: object) -> mic.JsonObject:
            assert source is handle
            calls["estimate"] += 1
            return {"provider": "bigquery", "estimated_bytes_processed": 42, "dry_run": True}

    module = ModuleType("_mic_estimate_fixture")
    module.data = data
    module.evaluation = evaluation
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(cli, "default_resolver", FakeResolver)
    assert cli.main(["estimate", f"{module.__name__}:{symbol}"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["estimate"]["estimated_bytes_processed"] == 42
    assert result["tasks_executed"] == result["rows_read"] == 0
    assert calls == {"factory": 1, "estimate": 1, "task": 0}


def test_estimate_unsupported_provider_is_clear() -> None:
    result = invoke("estimate", "examples.triage:triage_data")
    assert result.returncode == 2
    assert "does not support cost estimation" in result.stderr.lower()
