"""Coverage gates fail visibly on regressions and missing or malformed evidence."""

import json
import subprocess

import pytest

from scripts import check_coverage, verify


def report(lines=93, branches=84, total=100, branch_total=100):
    return {
        "totals": {
            "covered_lines": lines,
            "num_statements": total,
            "covered_branches": branches,
            "num_branches": branch_total,
        }
    }


@pytest.mark.parametrize(
    "lines,branches,passed", [(93, 84, True), (92, 84, False), (93, 83, False), (100, 100, True)]
)
def test_independent_floors(lines, branches, passed):
    result = check_coverage.evaluate(report(lines, branches))
    assert result["passed"] is passed
    assert result["statements"]["minimum_percent"] == 93
    assert result["branches"]["minimum_percent"] == 84


def test_display_rounding_does_not_hide_regression():
    assert not check_coverage.evaluate(report(9299, 8400, 10000, 10000))["passed"]


def test_no_branches_is_vacuously_covered_but_no_statements_is_not_evidence():
    assert check_coverage.evaluate(report(branches=0, branch_total=0))["branches"]["percent"] == 100
    with pytest.raises(ValueError, match="No statements"):
        check_coverage.evaluate(report(0, 0, 0, 0))


@pytest.mark.parametrize("value", [-1, True, 1.5, "93", None, 101])
def test_invalid_counts(value):
    with pytest.raises(ValueError):
        check_coverage.evaluate(report(lines=value))


@pytest.mark.parametrize("content", [None, "not json", "{}", '{"totals": null}'])
def test_bad_evidence_fails_and_replaces_previous_result(tmp_path, content):
    source, output = tmp_path / "coverage.json", tmp_path / "gates.json"
    output.write_text('{"passed": true}', encoding="utf-8")
    if content is not None:
        source.write_text(content, encoding="utf-8")
    assert check_coverage.main([str(source), "--output", str(output)]) == 1
    assert json.loads(output.read_text())["passed"] is False


def test_checker_success_records_measurements(tmp_path):
    source, output = tmp_path / "coverage.json", tmp_path / "gates.json"
    source.write_text(json.dumps(report()), encoding="utf-8")
    assert check_coverage.main([str(source), "--output", str(output)]) == 0
    assert json.loads(output.read_text())["branches"]["covered"] == 84


def test_verifier_cannot_reuse_old_coverage_when_collection_fails(tmp_path, monkeypatch):
    root = tmp_path
    (root / "scripts").mkdir()
    (root / "pyproject.toml").write_text("", encoding="utf-8")
    (root / "uv.lock").write_text("", encoding="utf-8")
    evidence = root / ".artifacts/verification"
    evidence.mkdir(parents=True)
    for name in (".coverage", "coverage.json", "coverage-gates.json", "tests.xml"):
        (evidence / name).write_text("stale", encoding="utf-8")
    monkeypatch.setattr(verify, "__file__", str(root / "scripts/verify.py"))

    def failed(command, **kwargs):
        assert not (evidence / "coverage.json").exists()
        assert not (evidence / ".coverage").exists()
        return subprocess.CompletedProcess(command, 1, "simulated failure", "")

    monkeypatch.setattr(verify.subprocess, "run", failed)
    assert verify.main() == 1
    result = json.loads((evidence / "verification.json").read_text())
    assert (
        next(check for check in result["checks"] if check["name"] == "coverage_gates")["exit_code"]
        == 1
    )
