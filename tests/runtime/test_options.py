"""Evaluation options behavior through the real runner."""

from dataclasses import replace

import pytest

import mic

from .helpers import evaluation, row, scores


def test_low_score_and_explicit_gate_have_distinct_exit_codes(tmp_path):
    spec = evaluation([row()], task=lambda _ctx, _value: 0)
    low = mic.run(spec, output=tmp_path / "low")
    assert low.exit_code == 0
    assert low.status == "completed"
    assert scores(low)["exact"].mean == 0
    gated = mic.run(
        spec, output=tmp_path / "gated", require=['tasks["runtime"].scores["exact"].mean >= 0.9']
    )
    assert gated.exit_code == 1
    assert gated.summary.trials.task_failed == 0
    assert gated.requirements[0].passed is False


def test_result_version_and_summary_only_shape(tmp_path):
    result = mic.run(evaluation([row()]), output=tmp_path)
    assert result.to_json()["schema_version"] == "mic-run-v4"
    assert not hasattr(result, "cases")
    assert "p95" not in result.to_json()["summary"]["trials"]["total_ms"]


@pytest.mark.parametrize(
    "setting,value",
    [
        ("trials", 0),
        ("trials", True),
        ("trials", 1.5),
        ("concurrency", -1),
        ("max_executions", 0),
        ("timeout", float("nan")),
    ],
)
def test_invalid_execution_config_never_calls_source_or_task(tmp_path, setting, value):
    calls = []
    spec = evaluation([row()], task=lambda _value: calls.append("task"))
    spec = replace(spec, dataset=replace(spec.dataset, factory=lambda: calls.append("source")))
    with pytest.raises(mic.ConfigurationError):
        mic.run(spec, output=tmp_path, **{setting: value})
    assert calls == []
    assert not (tmp_path / "run.json").exists()


@pytest.mark.parametrize(
    "require", [["missing>=1"], ["exact >= nan"], ["exact;boom"], ["exact>=1e999"]]
)
def test_invalid_gate_never_calls_task(tmp_path, require):
    calls = []
    with pytest.raises(mic.ConfigurationError):
        mic.run(
            evaluation([row()], task=lambda _value: calls.append(1)),
            output=tmp_path,
            require=require,
        )
    assert not calls


def test_duplicate_scorer_name_rejected_before_data_read(tmp_path):
    spec = evaluation([row()])
    spec = replace(spec, scorers=(spec.scorers[0], spec.scorers[0]))
    with pytest.raises(mic.ConfigurationError, match="Duplicate scorer"):
        mic.run(spec, output=tmp_path)


def test_empty_dataset_and_null_only_metric_do_not_pass_a_gate(tmp_path):
    result = mic.run(
        evaluation([]),
        output=tmp_path / "empty",
        require=['tasks["runtime"].scores["exact"].mean >= 0'],
    )
    assert result.exit_code == 1
    assert scores(result)["exact"].mean is None

    @mic.scorer(name="maybe", requires_expected=False)
    def maybe(ctx):
        return None

    result = mic.run(
        evaluation([row()], scorers=[maybe]),
        output=tmp_path / "null",
        require=['tasks["runtime"].scores["maybe"].mean >= 0'],
    )
    assert scores(result)["maybe"] == mic.Statistics(0, None, None, None)
    assert result.summary.trials.scoring_skipped == 1
    assert result.exit_code == 1


async def test_sync_entrypoints_give_async_host_instructions():
    with pytest.raises(mic.ConfigurationError, match="await mic.arun"):
        mic.run(evaluation([row()]))
    with pytest.raises(mic.ConfigurationError, match="await mic.apreflight"):
        mic.preflight(evaluation([row()]))
    with pytest.raises(mic.ConfigurationError, match="await mic.ainspect_dataset"):
        mic.inspect_dataset(evaluation([row()]).dataset)


def test_resource_caps_stop_admission_and_preserve_completed_work(tmp_path):
    result = mic.run(evaluation([row(), row(2)], trials=2), output=tmp_path, max_executions=3)
    assert result.exit_code == 2
    assert result.summary.trials.planned == result.summary.trials.completed == 3


async def test_apreflight_reads_source_once_and_executes_no_callbacks():
    calls = []
    spec = evaluation([row()], task=lambda _value: calls.append("task"))
    spec = replace(
        spec, dataset=replace(spec.dataset, factory=lambda: calls.append("source") or [row()])
    )
    ready = await mic.apreflight(spec)
    assert ready["tasks_executed"] == 0
    assert calls == ["source"]


def test_sync_preflight_and_inspection_use_blocking_entrypoints():
    spec = evaluation([row()])
    ready = mic.preflight(spec)
    inspected = mic.inspect_dataset(spec.dataset, limits=mic.ReadLimits(row_count=1))
    assert ready["tasks_executed"] == 0
    assert inspected["dataset"]["records_accepted"] == 1


async def test_async_dataset_inspection_uses_prefixed_entrypoint():
    inspected = await mic.ainspect_dataset(
        evaluation([row()]).dataset, limits=mic.ReadLimits(row_count=1)
    )
    assert inspected["dataset"]["records_accepted"] == 1


def test_required_scorer_rejects_unlabeled_cases_before_execution(tmp_path):
    calls = []
    schema = mic.case_schema(input=int, expected=int, expected_policy="optional")
    result = mic.run(
        evaluation([{"input": 1}], schema=schema, task=lambda _value: calls.append(1)),
        output=tmp_path,
    )
    assert result.exit_code == 2
    assert calls == []
