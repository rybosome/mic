"""Configuration and filesystem failures cannot masquerade as running evaluations."""

import json
from dataclasses import replace

import pytest

import mic

from .helpers import evaluation, manifest, row


@pytest.mark.parametrize("component", ["input", "output"])
def test_broken_schema_description_fails_before_source_access(tmp_path, component):
    calls = []

    class BrokenSchema:
        def validate(self, value, *, strict=True):
            return value

        def dump(self, value):
            return value

        def json_schema(self):
            raise ValueError("schema description unavailable")

    spec = evaluation([row()])
    spec = replace(
        spec, dataset=replace(spec.dataset, __wrapped__=lambda: calls.append("source") or [row()])
    )
    if component == "input":
        spec = replace(
            spec,
            dataset=replace(
                spec.dataset, schema=replace(spec.dataset.schema, input=BrokenSchema())
            ),
        )
    else:
        spec = replace(spec, output=BrokenSchema())
    with pytest.raises(mic.ConfigurationError, match="schema description unavailable"):
        mic.run(spec, output=tmp_path)
    assert calls == []
    assert manifest(tmp_path)["status"] == "failed"
    assert manifest(tmp_path)["exit_code"] == 2


def test_noncallable_scorer_fails_before_source_access(tmp_path):
    calls = []
    spec = evaluation([row()])
    spec = replace(
        spec,
        dataset=replace(spec.dataset, __wrapped__=lambda: calls.append("source") or [row()]),
        scorers=(replace(spec.scorers[0], __wrapped__=None),),
    )
    with pytest.raises(mic.ConfigurationError, match="scorer.*callable"):
        mic.run(spec, output=tmp_path)
    assert calls == []
    assert manifest(tmp_path)["exit_code"] == 2


def test_dataset_snapshot_write_failure_saves_failed_manifest_without_tasks(tmp_path):
    calls = []
    spec = evaluation([row()], task=lambda *_: calls.append("task"))

    def source():
        snapshot = tmp_path / "dataset.jsonl"
        snapshot.unlink()
        snapshot.mkdir()
        return [row()]

    spec = replace(spec, dataset=replace(spec.dataset, __wrapped__=source))
    result = mic.run(spec, output=tmp_path)
    assert calls == []
    assert result.exit_code == 1
    saved = manifest(tmp_path)
    assert saved["status"] == "failed"
    assert saved["failures"][0]["phase"] == "artifact"
    assert saved["failures"][0]["type"] in {"IsADirectoryError", "PermissionError"}
    assert (tmp_path / "report.html").is_file()


def test_journal_write_failure_stops_admission_and_retains_local_failure(tmp_path):
    calls = []

    def task(_, value):
        calls.append(value)
        journal = tmp_path / "cases.jsonl"
        journal.unlink()
        journal.mkdir()
        return value

    result = mic.run(
        evaluation([row(1, id="one"), row(2, id="two")], task=task, concurrency=1), output=tmp_path
    )
    assert result.exit_code == 1
    assert calls == [1]
    saved = manifest(tmp_path)
    assert saved["status"] == "failed"
    assert saved["failures"][-1]["phase"] == "artifact"
    assert saved["counts"]["completed"] == 1
    assert saved["counts"]["cancelled"] == 1


def test_run_artifacts_include_the_declared_output_schema(tmp_path):
    result = mic.run(evaluation([row()], output=int), output=tmp_path)
    assert result.exit_code == 0
    assert json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))["output_schema"] == {
        "type": "integer"
    }
