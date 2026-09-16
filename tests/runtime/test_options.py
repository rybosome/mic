"""Evaluation options behavior through the real runner."""

from dataclasses import replace

import pytest

import mic

from .helpers import evaluation, manifest, row


def test_low_score_and_explicit_gate_have_distinct_exit_codes(tmp_path):
    spec = evaluation([row()], task=lambda _ctx, _value: 0)
    low = mic.run(spec, output=tmp_path / "low")
    assert low.exit_code == 0
    assert low.status == "completed"
    assert low.manifest["scores"]["exact"]["mean"] == 0
    gated = mic.run(spec, output=tmp_path / "gated", require=["exact>=0.9"])
    assert gated.exit_code == 1
    assert gated.manifest["counts"]["failed"] == 0
    assert gated.manifest["gates"][0]["passed"] is False


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
    spec = evaluation([row()], task=lambda *args: calls.append("task"))
    spec = replace(spec, dataset=replace(spec.dataset, __wrapped__=lambda: calls.append("source")))
    with pytest.raises(mic.ConfigurationError):
        mic.run(spec, output=tmp_path, **{setting: value})
    assert calls == []
    assert manifest(tmp_path)["exit_code"] == 2


@pytest.mark.parametrize(
    "require", [["missing>=1"], ["exact >= nan"], ["exact;boom"], ["exact>=1e999"]]
)
def test_invalid_gate_never_calls_task(tmp_path, require):
    calls = []
    with pytest.raises(mic.ConfigurationError):
        mic.run(
            evaluation([row()], task=lambda *_: calls.append(1)), output=tmp_path, require=require
        )
    assert not calls


def test_duplicate_declared_metric_rejected_before_data_read(tmp_path):
    spec = evaluation([row()])
    spec = replace(spec, scorers=(spec.scorers[0], replace(spec.scorers[0], name="other")))
    with pytest.raises(mic.ConfigurationError, match="Duplicate metric"):
        mic.run(spec, output=tmp_path)


def test_empty_dataset_and_null_only_metric_do_not_pass_a_gate(tmp_path):
    result = mic.run(evaluation([]), output=tmp_path / "empty", require=["exact>=0"])
    assert result.exit_code == 1
    assert result.manifest["scores"]["exact"]["mean"] is None

    @mic.scorer(name="maybe", requires_expected=False)
    def maybe(ctx):
        return mic.Score("maybe", None)

    result = mic.run(
        evaluation([row()], scorers=[maybe]), output=tmp_path / "null", require=["maybe>=0"]
    )
    assert result.manifest["scores"]["maybe"] == {
        "count": 0,
        "mean": None,
        "min": None,
        "max": None,
        "p50": None,
        "p95": None,
        "null_count": 1,
        "unavailable_count": 0,
    }
    assert result.exit_code == 1


async def test_library_sync_entrypoint_gives_notebook_instruction():
    with pytest.raises(mic.ConfigurationError, match="await mic.arun"):
        mic.run(evaluation([row()]))


def test_skip_does_not_validate_or_touch_source(tmp_path):
    def forbidden():
        raise AssertionError("source must not run")

    spec = evaluation([row()], skip=True, trials=0)
    spec = replace(spec, dataset=replace(spec.dataset, __wrapped__=forbidden))
    result = mic.run(spec, output=tmp_path)
    assert result.status == "skipped"
    assert result.exit_code == 0


def test_resource_caps_prevent_all_task_execution(tmp_path):
    calls = []
    spec = evaluation([row(), row(2, id="b")], task=lambda *_: calls.append(1), trials=2)
    with pytest.raises(mic.ConfigurationError, match="max_executions"):
        mic.run(spec, output=tmp_path, max_executions=3)
    assert calls == []
    assert manifest(tmp_path)["exit_code"] == 2


async def test_preflight_reads_source_once_and_executes_no_callbacks():
    calls = []
    spec = evaluation([row()], task=lambda *_: calls.append("task"))
    spec = replace(
        spec, dataset=replace(spec.dataset, __wrapped__=lambda: calls.append("source") or [row()])
    )
    ready = await mic.preflight(spec)
    assert ready["tasks_executed"] == 0
    assert calls == ["source"]


def test_required_scorer_rejects_unlabeled_cases_before_execution(tmp_path):
    calls = []
    schema = mic.case_schema(input=int, expected=int, expected_policy="optional")
    with pytest.raises(mic.DatasetError, match="require expected"):
        mic.run(
            evaluation([{"input": 1}], schema=schema, task=lambda *_: calls.append(1)),
            output=tmp_path,
        )
    assert calls == []
