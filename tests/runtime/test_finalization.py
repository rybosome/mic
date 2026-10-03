"""Filesystem faults preserve computed work and never replay side effects."""

import asyncio
import errno
import json
from pathlib import Path

import pytest

import mic
from mic._runtime import files, provenance

from .helpers import cases, evaluation, manifest, row
from .test_streaming import Capture


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


@pytest.mark.parametrize("once", [False, True])
def test_terminal_failure_preserves_work_without_replaying_sinks(tmp_path, monkeypatch, once):
    failed = fail_replacement(monkeypatch, {"run.json"}, once=once)
    sink = Capture()
    calls = []
    result = mic.run(
        evaluation([row()], task=lambda value: calls.append(value) or value),
        output=tmp_path,
        sinks=[sink],
    )
    assert result.exit_code == 1
    assert calls == [1] and len(sink.trials) == 1 and sink.closed
    assert cases(result)[0]["scores"][0]["value"] == 1
    assert len(failed) == (1 if once else 2)
    assert result.sinks[-1].name == "manifest"
    assert manifest(tmp_path)["status"] == ("failed" if once else "running")
    assert not list(tmp_path.glob(".mic-*"))


@pytest.mark.parametrize("stage", ["directory", "journal", "manifest"])
def test_initialization_failure_starts_no_tasks_or_later_sinks(tmp_path, monkeypatch, stage):
    destination = tmp_path / "run"
    calls = []
    sink = Capture()
    if stage == "manifest":
        fail_replacement(monkeypatch, {"run.json"}, terminal_only=False)
    else:
        method = "mkdir" if stage == "directory" else "open"
        original = getattr(Path, method)

        def fail(path, *args, **kwargs):
            if path == (destination if stage == "directory" else destination / "events.jsonl"):
                raise PermissionError("private filesystem detail")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, method, fail)
    result = mic.run(
        evaluation([row()], task=lambda value: calls.append(value)),
        output=destination,
        sinks=[sink],
    )
    assert result.exit_code == 1
    assert calls == [] and sink.events == []
    assert result.sinks[0].error.phase == "sink_open"
    assert "private filesystem" not in json.dumps(result.to_json())


async def test_cancellation_survives_manifest_failure(tmp_path, monkeypatch):
    entered = asyncio.Event()

    async def task(value):
        entered.set()
        await asyncio.Event().wait()

    running = asyncio.create_task(mic.arun(evaluation([row()], task=task), output=tmp_path))
    await asyncio.wait_for(entered.wait(), 2)
    fail_replacement(monkeypatch, {"run.json"}, once=True)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    saved = manifest(tmp_path)
    assert saved["status"] == "cancelled" and saved["exit_code"] == 130
    assert saved["sinks"][-1]["name"] == "manifest"
    assert saved["summary"]["trials"]["cancelled"] >= 1


def test_journal_failure_does_not_erase_summary_or_repeat_work(tmp_path, monkeypatch):
    from mic._runtime import recorder

    original = recorder._Session.write
    calls = []

    async def fail(self, event):
        from mic.sinks import TrialFinished

        if isinstance(event, TrialFinished):
            raise OSError(errno.ENOSPC, "full disk")
        await original(self, event)

    monkeypatch.setattr(recorder._Session, "write", fail)
    sink = Capture()
    result = mic.run(
        evaluation([row()], task=lambda value: calls.append(value) or value),
        output=tmp_path,
        sinks=[sink],
    )
    assert result.exit_code == 1 and calls == [1]
    assert result.summary.trials.completed == 1
    assert len(sink.trials) == 1
    assert manifest(tmp_path) == result.to_json()


def test_failed_directory_check_never_overwrites_evidence(tmp_path, monkeypatch):
    destination = tmp_path / "run.json"
    destination.write_text("existing evidence", encoding="utf-8")
    original = Path.iterdir

    def denied(path):
        if path == tmp_path:
            raise PermissionError("cannot inspect destination")
        return original(path)

    monkeypatch.setattr(Path, "iterdir", denied)
    with pytest.raises(PermissionError):
        mic.run(evaluation([row()]), output=tmp_path)
    assert destination.read_text() == "existing evidence"


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


def test_framework_hash_includes_package_code_outside_runtime(tmp_path, monkeypatch):
    package = tmp_path / "mic"
    runtime = package / "_runtime"
    runtime.mkdir(parents=True)
    source = runtime / "provenance.py"
    source.write_text("# runtime", encoding="utf-8")
    schema = package / "schema.py"
    schema.write_text("# schema version one", encoding="utf-8")
    monkeypatch.setattr(provenance, "__file__", str(source))
    first = provenance.code_provenance(object())["framework_sha256"]
    schema.write_text("# schema version two", encoding="utf-8")
    second = provenance.code_provenance(object())["framework_sha256"]
    assert first != second
    assert second == provenance.code_provenance(object())["framework_sha256"]
