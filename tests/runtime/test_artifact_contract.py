"""The published format accepts real lifecycle states and rejects malformed records."""

import asyncio
import copy
import json
import re
from dataclasses import replace

import pytest
from referencing.exceptions import Unresolvable

import mic

from .artifact_contract import assert_artifact, assert_directory, validators
from .helpers import evaluation, row
from .test_finalization import RecordingReporter, fail_replacement


def test_schema_documents_are_valid_and_references_are_offline():
    assert set(validators()) == {"common-v2", "run-v2", "dataset-row-v2", "case-record-v2"}
    validator = validators()["run-v2"]
    with pytest.raises(Unresolvable):
        validator.evolve(schema={"$ref": "https://invalid.example/absent.schema.json"}).validate({})


def test_successful_artifacts_agree_across_snapshot_journal_and_html(tmp_path):
    result = mic.run(
        evaluation(
            [
                row(metadata={"tags": ["synthetic"]}, provenance={"source": {"record": 1}}),
                row(2, id="b"),
            ]
        ),
        output=tmp_path,
        trials=3,
    )
    manifest, rows, cases = assert_directory(tmp_path)
    assert manifest == result.manifest
    assert manifest["counts"] == {"planned": 6, "completed": 6, "failed": 0, "cancelled": 0}
    assert {(case["row_index"], case["trial"]) for case in cases} == {
        (index, trial) for index in range(2) for trial in range(1, 4)
    }
    for case in cases:
        assert case["case_id"] == rows[case["row_index"]]["id"]
        assert case["input"] == rows[case["row_index"]]["input"]
        if case["row_index"] == 0:
            assert case["metadata"] == {"tags": ["synthetic"]}
            assert case["provenance"] == {"source": {"record": 1}}
    assert manifest["scores"]["exact"]["count"] == 6
    assert manifest["scores"]["exact"]["mean"] == 1
    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    payload = json.loads(
        re.search(r'<script[^>]+type="application/json"[^>]*>(.*?)</script>', html, re.S)[1]
    )
    assert payload == {"manifest": manifest, "cases": result.cases}


def test_missing_null_and_extension_data_remain_distinct(tmp_path):
    @mic.scorer(name="nullable", requires_expected=False)
    def nullable(ctx):
        return mic.Score(None, {"nested": [None, {"free": True}]})

    class CustomReporter(RecordingReporter):
        async def report(self, manifest, cases):
            return {"custom": {"no_status_required": [1, None]}}

    spec = evaluation(
        [{"id": "missing", "input": None}, {"id": "null", "input": None, "expected": None}],
        schema=mic.case_schema(input=object, expected=object, expected_policy="optional"),
        scorers=[nullable],
        task=lambda _value: mic.TaskResult(None, {"custom": [True, None]}),
    )
    result = mic.run(spec, output=tmp_path, reporters=[CustomReporter()])
    manifest, rows, cases = assert_directory(tmp_path)
    assert result.exit_code == 0
    cases.sort(key=lambda case: case["row_index"])
    assert "expected" not in rows[0] and "expected" not in cases[0]
    assert rows[1]["expected"] is None and cases[1]["expected"] is None
    assert all("output" in case and case["output"] is None for case in cases)
    assert manifest["scores"]["nullable"]["null_count"] == 2
    assert manifest["scores"]["nullable"]["unavailable_count"] == 0


@pytest.mark.parametrize(
    "phase", ["task", "schema", "scorer", "gate", "reporter", "configuration", "dataset", "empty"]
)
def test_failure_and_empty_run_contracts(tmp_path, phase):
    def broken(*args):
        raise ValueError("synthetic failure")

    spec = evaluation([row()])
    kwargs = {}
    if phase == "task":
        spec = replace(spec, function=broken)
    elif phase == "schema":
        spec = evaluation([row()], output=int, task=lambda _value: "invalid integer")
    elif phase == "scorer":
        spec = replace(spec, scorers=(*spec.scorers, mic.scorer(name="broken")(broken)))
    elif phase == "gate":
        kwargs["require"] = ["exact>1"]
    elif phase == "reporter":
        kwargs["reporters"] = [RecordingReporter(broken)]
    elif phase == "configuration":
        kwargs["trials"] = 0
    elif phase == "dataset":
        spec = replace(spec, dataset=replace(spec.dataset, factory=broken))
    elif phase == "empty":
        spec = evaluation([])
    if phase in {"configuration", "dataset"}:
        with pytest.raises((mic.ConfigurationError, mic.DatasetError)):
            mic.run(spec, output=tmp_path, **kwargs)
    else:
        result = mic.run(spec, output=tmp_path, **kwargs)
        assert result.exit_code == (0 if phase == "empty" else 1)
    manifest, rows, cases = assert_directory(tmp_path)
    if phase == "scorer":
        assert cases[0]["scores"][0]["value"] == 1
        assert cases[0]["errors"][0]["scorer"] == "broken"
    if phase == "gate":
        assert manifest["failures"] == [] and manifest["gates"][0]["passed"] is False
    if phase == "reporter":
        assert_artifact("common-v2#/$defs/reporterFailure", manifest["reporting"]["recording"])
    if phase in {"configuration", "dataset"}:
        assert rows == cases == [] and manifest["exit_code"] == 2


@pytest.mark.parametrize("phase", ["dataset", "task", "reporter"])
async def test_cancelled_contracts_include_unstarted_work(tmp_path, phase):
    entered = asyncio.Event()

    async def wait(*args):
        entered.set()
        await asyncio.Event().wait()

    spec = evaluation(
        [row(), row(2, id="b")], task=(lambda _value: wait()) if phase == "task" else None
    )
    reporters = []
    if phase == "dataset":
        spec = replace(spec, dataset=replace(spec.dataset, factory=wait))
    if phase == "reporter":
        reporter = RecordingReporter()
        reporter.report = wait
        reporters = [reporter]
    running = asyncio.create_task(
        mic.arun(spec, output=tmp_path, reporters=reporters, concurrency=1)
    )
    await asyncio.wait_for(entered.wait(), 2)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    manifest, _, cases = assert_directory(tmp_path)
    assert manifest["exit_code"] == 130
    if phase == "task":
        assert len(cases) == 1
        assert manifest["counts"]["cancelled"] == 2  # Includes unstarted work.
    if phase == "reporter":
        assert manifest["counts"]["completed"] == 2
        assert manifest["reporting"]["recording"]["status"] == "cancelled"
        assert_artifact("common-v2#/$defs/reporterCancellation", manifest["reporting"]["recording"])


@pytest.mark.parametrize("names", [{"run.json"}, {"report.html"}, {"run.json", "report.html"}])
def test_persistence_faults_leave_valid_individual_records(tmp_path, monkeypatch, names):
    fail_replacement(monkeypatch, names)
    result = mic.run(evaluation([row()]), output=tmp_path)
    assert_artifact("run-v2", result.manifest)
    saved, _, _ = assert_directory(tmp_path)
    assert result.exit_code == 1
    if "run.json" in names:
        assert saved["status"] == "running"  # Valid, but not a terminal outcome.


@pytest.mark.parametrize("stage", ["snapshot", "journal", "reporter-outcome"])
def test_partial_evidence_schema_does_not_require_cross_file_equality(tmp_path, monkeypatch, stage):
    from mic._runtime import batch, engine

    def fail(*args):
        raise OSError("synthetic write failure")

    reporters = []
    if stage == "snapshot":
        monkeypatch.setattr(engine, "write_dataset", fail)
    elif stage == "journal":
        monkeypatch.setattr(batch, "append_case", fail)
    else:
        reporters = [RecordingReporter(lambda: fail_replacement(monkeypatch, {"run.json"}))]
    result = mic.run(evaluation([row()]), output=tmp_path, reporters=reporters)
    assert_artifact("run-v2", result.manifest)
    saved, rows, cases = assert_directory(tmp_path)
    assert result.exit_code == 1
    if stage == "snapshot":
        assert saved["dataset"]["rows"] == 1 and rows == []
    elif stage == "journal":
        assert len(result.cases) == 1 and cases == []
        assert_artifact("case-record-v2", result.cases[0])
    else:
        assert saved["reporting"] == {}
        assert result.manifest["reporting"]["recording"]["status"] == "completed"


async def test_journal_completion_order_is_not_snapshot_order(tmp_path):
    second = asyncio.Event()

    async def task(ctx, value):
        if value == 1:
            await second.wait()
        else:
            second.set()
        return value

    result = await mic.arun(
        evaluation([row(), row(2, id="b")], task=task, scorers=[]), output=tmp_path
    )
    _, _, cases = assert_directory(tmp_path)
    assert [case["case_id"] for case in cases] == ["b", "a"]
    assert [case["case_id"] for case in result.cases] == ["a", "b"]


@pytest.fixture
def valid_records(tmp_path):
    mic.run(evaluation([row()]), output=tmp_path)
    manifest, rows, cases = assert_directory(tmp_path)
    return {"run-v2": manifest, "dataset-row-v2": rows[0], "case-record-v2": cases[0]}


@pytest.mark.parametrize(
    "name,path,value",
    [
        ("run-v2", ("schema_version",), "mic-run-v999"),
        ("run-v2", ("status",), "unknown"),
        ("run-v2", ("exit_code",), 130),
        ("run-v2", ("counts", "planned"), -1),
        ("run-v2", ("ended_at",), "not-a-date"),
        ("run-v2", ("dataset", "unexpected"), True),
        ("case-record-v2", ("scores", 0, "value"), True),
        ("case-record-v2", ("metadata",), []),
        ("case-record-v2", ("trial",), 0),
        ("case-record-v2", ("latency", "task_ms"), -1),
        ("dataset-row-v2", ("id",), " "),
        ("dataset-row-v2", ("unexpected",), None),
    ],
)
def test_invalid_field_values_are_rejected(valid_records, name, path, value):
    record = copy.deepcopy(valid_records[name])
    target = record
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(AssertionError, match=r"\$"):
        assert_artifact(name, record)


@pytest.mark.parametrize(
    "name,field", [("run-v2", "counts"), ("dataset-row-v2", "input"), ("case-record-v2", "output")]
)
def test_required_fields_cannot_disappear(valid_records, name, field):
    del valid_records[name][field]
    with pytest.raises(AssertionError, match=field):
        assert_artifact(name, valid_records[name])


def test_nonfinite_extension_values_are_not_json(valid_records):
    valid_records["dataset-row-v2"]["input"] = {"value": float("nan")}
    with pytest.raises(ValueError):
        assert_artifact("dataset-row-v2", valid_records["dataset-row-v2"])
