"""Public streaming formats validate real lifecycle outcomes offline."""

import asyncio
import copy
from dataclasses import replace

import pytest
from referencing.exceptions import Unresolvable

import mic

from .artifact_contract import assert_artifact, assert_directory, validators
from .helpers import evaluation, row
from .test_finalization import fail_replacement


def test_schema_documents_are_valid_and_offline():
    assert set(validators()) == {"common-v3", "run-v3", "event-v1"}
    with pytest.raises(Unresolvable):
        validators()["run-v3"].evolve(schema={"$ref": "https://invalid.example/absent"}).validate(
            {}
        )


def test_events_and_summary_agree(tmp_path):
    result = mic.run(evaluation([row(), row(2)], task=lambda x: x), output=tmp_path, trials=3)
    saved, events = assert_directory(tmp_path)
    assert saved == result.to_json()
    accepted = {e["case_id"]: e["case"] for e in events if e["type"] == "case_accepted"}
    trials = [e for e in events if e["type"] == "trial_finished"]
    assert len(trials) == saved["summary"]["trials"]["completed"] == 6
    assert {(e["row_index"], e["trial"]) for e in trials} == {
        (r, t) for r in range(2) for t in range(1, 4)
    }
    for e in trials:
        assert e["result"]["input"] == accepted[e["case_id"]]["input"]


def test_missing_null_and_extension_data_remain_distinct(tmp_path):
    @mic.scorer(requires_expected=False)
    def nullable(ctx):
        return mic.Score(None, {"nested": [None, {"free": True}]})

    spec = evaluation(
        [{"input": None}, {"input": None, "expected": None}],
        schema=mic.case_schema(input=object, expected=object, expected_policy="optional"),
        scorers=[nullable],
        task=lambda value: mic.TaskResult(None, {"custom": [True, None]}),
    )
    result = mic.run(spec, output=tmp_path)
    _, events = assert_directory(tmp_path)
    cases = sorted(
        [e["result"] for e in events if e["type"] == "trial_finished"], key=lambda c: c["row_index"]
    )
    assert "expected" not in cases[0] and cases[1]["expected"] is None
    assert all("output" in c and c["output"] is None for c in cases)
    assert result.summary.trials.scoring_skipped == 2


@pytest.mark.parametrize("phase", ["task", "schema", "scorer", "gate", "dataset", "empty"])
def test_failure_contracts(tmp_path, phase):
    def broken(*args):
        raise ValueError("private")

    spec = evaluation([row()])
    kwargs = {}
    if phase == "task":
        spec = replace(spec, function=broken)
    if phase == "schema":
        spec = evaluation([row()], output=int, task=lambda value: "invalid")
    if phase == "scorer":
        spec = replace(spec, scorers=(*spec.scorers, mic.scorer(name="broken")(broken)))
    if phase == "gate":
        kwargs["require"] = ['tasks["runtime"].scores["exact"].mean > 1']
    if phase == "dataset":
        spec = replace(spec, dataset=replace(spec.dataset, factory=broken))
    if phase == "empty":
        spec = evaluation([])
    result = mic.run(spec, output=tmp_path, **kwargs)
    assert result.exit_code == (2 if phase == "dataset" else 1)
    assert_directory(tmp_path)


def test_persistence_failure_preserves_valid_running_marker(tmp_path, monkeypatch):
    fail_replacement(monkeypatch, {"run.json"})
    result = mic.run(evaluation([row()]), output=tmp_path)
    assert_artifact("run-v3", result.to_json())
    saved, _ = assert_directory(tmp_path)
    assert saved["status"] == "running"


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
    _, events = assert_directory(tmp_path)
    cases = [e["result"] for e in events if e["type"] == "trial_finished"]
    assert [case["label"] for case in cases] == ["b", "a"]
    assert result.summary.trials.completed == 2


@pytest.fixture
def valid_records(tmp_path):
    mic.run(evaluation([row()]), output=tmp_path)
    saved, events = assert_directory(tmp_path)
    return {"run-v3": saved, "event-v1": next(e for e in events if e["type"] == "trial_finished")}


@pytest.mark.parametrize(
    "name,path,value",
    [
        ("run-v3", ("schema_version",), "mic-run-v999"),
        ("run-v3", ("status",), "unknown"),
        ("run-v3", ("exit_code",), 130),
        ("run-v3", ("summary", "trials", "planned"), -1),
        ("run-v3", ("info", "started_at"), "not-a-date"),
        ("event-v1", ("result", "scores", 0, "value"), True),
        ("event-v1", ("trial",), 0),
        ("event-v1", ("result", "latency", "task_ms"), -1),
        ("event-v1", ("unexpected",), None),
    ],
)
def test_invalid_fields(valid_records, name, path, value):
    record = copy.deepcopy(valid_records[name])
    target = record
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(AssertionError):
        assert_artifact(name, record)


@pytest.mark.parametrize(
    "name,path",
    [
        ("run-v3", ("summary",)),
        ("event-v1", ("result", "input")),
        ("event-v1", ("result", "output")),
    ],
)
def test_required_fields(valid_records, name, path):
    record = valid_records[name]
    target = record
    for key in path[:-1]:
        target = target[key]
    del target[path[-1]]
    with pytest.raises(AssertionError):
        assert_artifact(name, record)


def test_nonfinite_extensions(valid_records):
    valid_records["event-v1"]["result"]["input"] = {"value": float("nan")}
    with pytest.raises(ValueError):
        assert_artifact("event-v1", valid_records["event-v1"])
