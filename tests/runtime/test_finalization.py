"""Filesystem faults preserve computed work, primary errors, and side-effect boundaries."""

import asyncio
import errno
import json
from dataclasses import replace
from pathlib import Path

import pytest

import mic
from mic._runtime import artifacts, files

from .helpers import evaluation, manifest, row


class RecordingReporter:
    name = "recording"

    def __init__(self, callback=None):
        self.calls = 0
        self.callback = callback

    async def prepare(self):
        pass

    async def report(self, manifest, cases):
        self.calls += 1
        if self.callback:
            self.callback()
        return {"status": "completed", "rows": len(cases)}


def fail_replacement(monkeypatch, names, *, terminal_only=True, once=False):
    original = files.os.replace
    failed = []

    def replace_file(source, destination):
        target = Path(destination)
        terminal = target.name != "run.json" or (
            json.loads(Path(source).read_text(encoding="utf-8"))["status"] != "running"
        )
        if target.name in names and (terminal or not terminal_only) and (not once or not failed):
            failed.append(target.name)
            raise OSError(errno.ENOSPC, "simulated full disk")
        return original(source, destination)

    monkeypatch.setattr(files.os, "replace", replace_file)
    return failed


@pytest.mark.parametrize("names", [{"run.json"}, {"report.html"}, {"run.json", "report.html"}])
def test_terminal_write_failure_retains_cases_and_blocks_export(tmp_path, monkeypatch, names):
    failed = fail_replacement(monkeypatch, names)
    reporter = RecordingReporter()
    calls = []
    result = mic.run(
        evaluation([row()], task=lambda _, value: calls.append(value) or value),
        output=tmp_path,
        reporters=[reporter],
    )
    assert result.exit_code == result.manifest["exit_code"] == 1
    assert result.status == "failed"
    assert calls == [1] and reporter.calls == 0
    assert result.cases[0]["output"] == 1
    assert result.cases[0]["scores"][0]["value"] == 1
    assert len(failed) == 2 * len(names)  # One initial attempt and one bounded recovery.
    assert all(error["phase"] == "artifact" for error in result.manifest["failures"])
    assert result.manifest["ended_at"] and result.manifest["duration_ms"] > 0
    assert not list(tmp_path.glob(".mic-*"))
    if "run.json" in names:
        assert manifest(tmp_path)["status"] == "running"  # Earlier complete file survives.
    else:
        assert manifest(tmp_path)["status"] == "failed"
        assert manifest(tmp_path)["failures"][0]["phase"] == "artifact"


def test_transient_report_failure_saves_updated_failure_in_both_files(tmp_path, monkeypatch):
    fail_replacement(monkeypatch, {"report.html"}, once=True)
    result = mic.run(evaluation([row()]), output=tmp_path)
    assert result.exit_code == 1
    assert manifest(tmp_path) == result.manifest
    assert "simulated full disk" in (tmp_path / "report.html").read_text(encoding="utf-8")
    assert len(result.manifest["failures"]) == 1


@pytest.mark.parametrize("stage", ["directory", "journal", "manifest"])
def test_initialization_failure_starts_no_tasks_or_exports(tmp_path, monkeypatch, stage):
    destination = tmp_path / "run"
    calls = []
    reporter = RecordingReporter()
    if stage == "manifest":
        fail_replacement(monkeypatch, {"run.json"}, terminal_only=False)
    else:
        method = "mkdir" if stage == "directory" else "touch"
        original = getattr(Path, method)

        def fail(path, *args, **kwargs):
            if path == (destination if stage == "directory" else destination / "cases.jsonl"):
                raise PermissionError("simulated permission failure")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, method, fail)
    result = mic.run(
        evaluation([row()], task=lambda _value: calls.append(True)),
        output=destination,
        reporters=[reporter],
    )
    assert result.exit_code == 1 and result.cases == []
    assert calls == [] and reporter.calls == 0
    assert result.manifest["failures"][0]["phase"] == "artifact"


@pytest.mark.parametrize("kind", ["configuration", "dataset"])
@pytest.mark.parametrize("names", [{"report.html"}, {"run.json", "report.html"}])
def test_write_failure_never_replaces_primary_setup_error(tmp_path, monkeypatch, kind, names):
    fail_replacement(monkeypatch, names)
    primary = (
        mic.ConfigurationError("original setup failure")
        if kind == "configuration"
        else (mic.DatasetError("original setup failure"))
    )
    spec = evaluation([row()])

    def fail_source():
        raise primary

    spec = replace(spec, dataset=replace(spec.dataset, factory=fail_source))
    with pytest.raises(type(primary)) as caught:
        mic.run(spec, output=tmp_path)
    assert caught.value is primary
    notes = "\n".join(primary.__notes__)
    assert "incomplete or stale" in notes and "simulated full disk" in notes
    assert "Local evidence:" not in notes
    if "run.json" not in names:
        assert manifest(tmp_path)["exit_code"] == 2
        assert manifest(tmp_path)["failures"][0]["message"] == "original setup failure"


@pytest.mark.parametrize("phase", ["dataset", "task", "reporter"])
async def test_cancellation_survives_persistence_failure(tmp_path, monkeypatch, phase):
    entered = asyncio.Event()
    cleaned = []

    async def wait(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(True)

    spec = evaluation([row()], task=(lambda _value: wait()) if phase == "task" else None)
    reporters = []
    if phase == "dataset":
        spec = replace(spec, dataset=replace(spec.dataset, factory=wait))
    if phase == "reporter":
        reporter = RecordingReporter()
        reporter.report = wait
        reporters.append(reporter)
    running = asyncio.create_task(mic.arun(spec, output=tmp_path, reporters=reporters))
    await asyncio.wait_for(entered.wait(), 2)
    fail_replacement(monkeypatch, {"report.html"})
    running.cancel("original cancellation")
    with pytest.raises(asyncio.CancelledError) as caught:
        await running
    assert cleaned == [True]
    assert "incomplete or stale" in "\n".join(caught.value.__notes__)
    saved = manifest(tmp_path)
    assert saved["status"] == "cancelled" and saved["exit_code"] == 130
    assert saved["ended_at"] and saved["duration_ms"] > 0
    assert any(error["phase"] == "artifact" for error in saved["failures"])
    if phase == "reporter":
        assert saved["reporting"]["recording"]["status"] == "cancelled"


@pytest.mark.parametrize("upload_fails", [False, True])
def test_export_outcome_write_failure_never_replays_or_starts_next_export(
    tmp_path, monkeypatch, upload_fails
):
    previous = {}

    def after_upload():
        previous["report"] = (tmp_path / "report.html").read_bytes()
        fail_replacement(monkeypatch, {"report.html"})
        if upload_fails:
            raise RuntimeError("original export failure")

    first = RecordingReporter(after_upload)
    second = RecordingReporter()
    second.name = "second"
    calls = []
    result = mic.run(
        evaluation([row()], task=lambda _, value: calls.append(value) or value),
        output=tmp_path,
        reporters=[first, second],
    )
    assert result.exit_code == 1
    assert first.calls == 1 and second.calls == 0 and calls == [1]
    assert result.manifest["reporting"]["recording"]["status"] == (
        "failed" if upload_fails else "completed"
    )
    assert manifest(tmp_path)["reporting"] == result.manifest["reporting"]
    assert (tmp_path / "report.html").read_bytes() == previous["report"]


def test_render_failure_is_recorded_without_losing_task_results(tmp_path, monkeypatch):
    from mic.reporters import html

    def broken_render(*args):
        raise RuntimeError("template rendering failed")

    monkeypatch.setattr(html, "render_report", broken_render)
    result = mic.run(evaluation([row()]), output=tmp_path)
    assert result.exit_code == 1 and result.cases[0]["output"] == 1
    assert "template rendering failed" in manifest(tmp_path)["failures"][0]["message"]


def test_partial_atomic_write_preserves_previous_file_and_primary_error(tmp_path, monkeypatch):
    destination = tmp_path / "dataset.jsonl"
    destination.write_text("previous snapshot", encoding="utf-8")
    primary = OSError(errno.ENOSPC, "partial write")

    def chunks():
        yield "partial new snapshot"
        raise primary

    with pytest.raises(OSError) as caught:
        files.atomic_write(destination, chunks())
    assert caught.value is primary
    assert destination.read_text(encoding="utf-8") == "previous snapshot"
    assert not list(tmp_path.glob(".mic-*"))

    def fail_cleanup(*args, **kwargs):
        raise PermissionError("cleanup denied")

    monkeypatch.setattr(Path, "unlink", fail_cleanup)
    with pytest.raises(OSError) as caught:
        files.atomic_write(destination, chunks())
    assert caught.value is primary
    assert "cleanup denied" in "\n".join(primary.__notes__)
    assert destination.read_text(encoding="utf-8") == "previous snapshot"


def test_failed_empty_directory_check_never_overwrites_existing_evidence(tmp_path, monkeypatch):
    destination = tmp_path / "run.json"
    destination.write_text("existing evidence", encoding="utf-8")
    original = Path.iterdir

    def denied(path):
        if path == tmp_path:
            raise PermissionError("cannot inspect destination")
        return original(path)

    monkeypatch.setattr(Path, "iterdir", denied)
    result = mic.run(evaluation([row()]), output=tmp_path)
    assert result.exit_code == 1
    assert destination.read_text(encoding="utf-8") == "existing evidence"
    assert not (tmp_path / "report.html").exists()


def test_stream_write_failure_preserves_old_destination(tmp_path, monkeypatch):
    destination = tmp_path / "run.json"
    destination.write_text("previous complete file", encoding="utf-8")
    original = files.tempfile.NamedTemporaryFile

    class PartialWriter:
        def __enter__(self):
            self.file = original(
                mode="w", encoding="utf-8", dir=tmp_path, prefix=".mic-", delete=False
            )
            self.name = self.file.name
            return self

        def write(self, chunk):
            self.file.write(chunk[:5])
            self.file.flush()
            raise OSError(errno.ENOSPC, "stream write failed")

        def __exit__(self, *args):
            self.file.close()

    monkeypatch.setattr(files.tempfile, "NamedTemporaryFile", lambda **kwargs: PartialWriter())
    with pytest.raises(OSError, match="stream write failed"):
        files.atomic_write(destination, ("new contents",))
    assert destination.read_text(encoding="utf-8") == "previous complete file"
    assert not list(tmp_path.glob(".mic-*"))


def test_journal_failure_blocks_export_even_when_final_manifest_is_saved(tmp_path, monkeypatch):
    from mic._runtime import batch

    def fail_append(*args):
        raise OSError(errno.ENOSPC, "journal full")

    monkeypatch.setattr(batch, "append_case", fail_append)
    reporter = RecordingReporter()
    result = mic.run(evaluation([row()]), output=tmp_path, reporters=[reporter])
    assert result.exit_code == 1 and reporter.calls == 0
    assert result.cases[0]["status"] == "completed"
    assert manifest(tmp_path) == result.manifest


def test_terminal_failure_preserves_prior_task_failure_and_success(tmp_path, monkeypatch):
    fail_replacement(monkeypatch, {"report.html"})

    def task(_, value):
        if value == 2:
            raise ValueError("original task failure")
        return value

    result = mic.run(evaluation([row(1, id="one"), row(2, id="two")], task=task), output=tmp_path)
    assert result.exit_code == 1
    assert result.cases[0]["scores"][0]["value"] == 1
    assert result.manifest["failures"][0]["message"] == "original task failure"
    assert result.manifest["failures"][1]["phase"] == "artifact"


def test_framework_hash_includes_package_code_outside_runtime(tmp_path, monkeypatch):
    package = tmp_path / "mic"
    runtime = package / "_runtime"
    runtime.mkdir(parents=True)
    source = runtime / "artifacts.py"
    source.write_text("# runtime", encoding="utf-8")
    schema = package / "schema.py"
    schema.write_text("# schema version one", encoding="utf-8")
    monkeypatch.setattr(artifacts, "__file__", str(source))
    first = artifacts.code_provenance(object())["framework_sha256"]
    schema.write_text("# schema version two", encoding="utf-8")
    second = artifacts.code_provenance(object())["framework_sha256"]
    assert first != second
    assert second == artifacts.code_provenance(object())["framework_sha256"]


def test_cli_does_not_advertise_failed_report(tmp_path, monkeypatch, capsys):
    from mic import cli

    fail_replacement(monkeypatch, {"report.html"})
    assert cli.main(["run", "examples.triage:baseline", "--output", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert "incomplete or stale" in output
    assert "Report       " not in output
