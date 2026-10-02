"""Saved report fidelity, escaping, and artifact safety."""

import json
import re
from pathlib import Path

import pytest

from mic.errors import ConfigurationError
from mic.reporters.console import format_summary
from mic.reporters.html import render_report, write_report

from ._fixtures import sample


def test_html_round_trip_is_offline_and_escaped() -> None:
    manifest, cases = sample()
    rendered = render_report(manifest, cases)
    payload = re.search(
        r'<script id="run-data" type="application/json">(.*?)</script>', rendered, re.S
    )
    assert payload
    decoded = json.loads(payload.group(1))
    assert decoded == {"manifest": manifest, "cases": cases}
    assert '<script>alert("x")</script>' not in rendered
    assert "<script>bad()</script>" not in rendered
    assert "default-src 'none'" in rendered
    assert "connect-src 'none'" in rendered
    assert "innerHTML" not in rendered
    assert 'role="tablist"' in rendered
    assert "ArrowDown" in rendered
    assert 'src="http' not in rendered


def test_saved_artifacts_render_without_eval_definition(tmp_path: Path) -> None:
    manifest, cases = sample()
    (tmp_path / "run.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "events.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "schema_version": "mic-event-v1",
                    "type": "trial_finished",
                    "task": case["task"],
                    "source_id": "source",
                    "result": case,
                }
            )
            for case in cases
        ),
        encoding="utf-8",
    )
    assert write_report(tmp_path) == tmp_path / "report.html"
    assert "Case details" in (tmp_path / "report.html").read_text(encoding="utf-8")
    manifest["schema_version"] = "unknown"
    (tmp_path / "run.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="Unsupported"):
        write_report(tmp_path)


def test_console_separates_quality_and_execution() -> None:
    manifest, _ = sample()
    text = format_summary(manifest)
    assert "2 completed / 2 admitted" in text
    assert "exact: mean=1.0 count=1" in text
    assert "0 task, 0 scoring" in text


@pytest.mark.parametrize("content", ["[]", '{"input":NaN}', '{"input":1e999}', "{not json}"])
def test_html_reader_rejects_invalid_case_json_without_pydantic(
    content: str, tmp_path: Path
) -> None:
    (tmp_path / "run.json").write_text(json.dumps(sample()[0]), encoding="utf-8")
    (tmp_path / "events.jsonl").write_text(content, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="Cannot render"):
        write_report(tmp_path)


def test_html_reader_rejects_non_object_manifest(tmp_path: Path) -> None:
    (tmp_path / "run.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="Cannot render"):
        write_report(tmp_path)


@pytest.mark.parametrize("name", ["run.json", "events.jsonl"])
@pytest.mark.parametrize("alias", ["direct", "symlink", "hardlink"])
def test_report_output_cannot_overwrite_source_artifacts(
    name: str, alias: str, tmp_path: Path
) -> None:
    manifest, cases = sample()
    (tmp_path / "run.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "events.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "schema_version": "mic-event-v1",
                    "type": "trial_finished",
                    "task": case["task"],
                    "source_id": "source",
                    "result": case,
                }
            )
            for case in cases
        ),
        encoding="utf-8",
    )
    (tmp_path / "dataset.jsonl").write_text('{"input":"original"}\n', encoding="utf-8")
    artifact = tmp_path / name
    original = artifact.read_bytes()
    destination = artifact
    if alias != "direct":
        destination = tmp_path / "alias.html"
        if alias == "symlink":
            destination.symlink_to(artifact)
        else:
            destination.hardlink_to(artifact)
    with pytest.raises(ConfigurationError, match="overwrite source artifact"):
        write_report(tmp_path, output=destination)
    assert artifact.read_bytes() == original


def test_failed_atomic_replacement_preserves_existing_report_and_cleans_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mic._runtime import files

    manifest, cases = sample()
    (tmp_path / "run.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "events.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "schema_version": "mic-event-v1",
                    "type": "trial_finished",
                    "task": case["task"],
                    "source_id": "source",
                    "result": case,
                }
            )
            for case in cases
        ),
        encoding="utf-8",
    )
    destination = tmp_path / "report.html"
    destination.write_text("previous complete report", encoding="utf-8")

    def fail_replace(source: Path, target: Path) -> None:
        assert source.parent == destination.parent
        assert source.read_text(encoding="utf-8").startswith("<!doctype html>")
        assert target == destination
        raise OSError("replacement unavailable")

    monkeypatch.setattr(files.os, "replace", fail_replace)
    with pytest.raises(ConfigurationError, match="Cannot render"):
        write_report(tmp_path)
    assert destination.read_text(encoding="utf-8") == "previous complete report"
    assert not list(tmp_path.glob(".mic-*"))


def test_csp_hash_matches_exact_executable_script() -> None:
    import base64
    import hashlib

    rendered = render_report(*sample())
    executable = re.search(r"<script>(.*?)</script>", rendered, re.S)
    assert executable
    digest = base64.b64encode(hashlib.sha256(executable.group(1).encode()).digest()).decode()
    assert f"script-src 'sha256-{digest}'" in rendered


def test_literal_template_markers_in_titles_and_case_values_are_not_substituted() -> None:
    manifest, cases = sample()
    name = "__DATA__ __SCRIPT__ __TITLE__ __SCRIPT_HASH__"
    manifest["name"] = name
    cases[0]["input"] = name
    rendered = render_report(manifest, cases)
    assert "<title>Mic evaluation · mic run review</title>" in rendered
    assert rendered.count('<script id="run-data"') == 1
    payload = re.search(
        r'<script id="run-data" type="application/json">(.*?)</script>', rendered, re.S
    )
    assert payload
    assert json.loads(payload.group(1))["cases"][0]["input"] == name


@pytest.mark.parametrize("cap", ["max_cases", "max_bytes", "manifest_bytes"])
def test_report_caps_fail_without_truncating_or_replacing_existing_report(tmp_path, cap):
    import mic
    from tests.runtime.helpers import evaluation, row

    mic.run(evaluation([row(), row(2)]), output=tmp_path)
    target = tmp_path / "report.html"
    target.write_text("prior report")
    limits = (
        {"max_cases": 1}
        if cap == "max_cases"
        else {
            "max_bytes": 1
            if cap == "manifest_bytes"
            else (tmp_path / "run.json").stat().st_size + 10
        }
    )
    with pytest.raises(ConfigurationError, match="Report exceeds"):
        write_report(tmp_path, **limits)
    assert target.read_text() == "prior report"


@pytest.mark.parametrize("change", ["running", "version", "event", "missing_result"])
def test_report_rejects_unfinalized_or_unrecognized_evidence(tmp_path, change):
    import mic
    from tests.runtime.helpers import evaluation, row

    mic.run(evaluation([row()]), output=tmp_path)
    if change == "running":
        path = tmp_path / "run.json"
        manifest = json.loads(path.read_text())
        manifest["status"] = "running"
        path.write_text(json.dumps(manifest))
    else:
        path = tmp_path / "events.jsonl"
        event = {"schema_version": "mic-event-v1", "type": "trial_finished"}
        if change == "version":
            event["schema_version"] = "wrong"
        if change == "event":
            event["type"] = "unknown"
        path.write_text(json.dumps(event))
    with pytest.raises(ConfigurationError):
        write_report(tmp_path)
    assert not (tmp_path / "report.html").exists()
