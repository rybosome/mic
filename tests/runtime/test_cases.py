"""Evaluation cases behavior through the real runner."""

import asyncio
import json
from dataclasses import dataclass
from typing import Annotated

import pytest
from pydantic import AfterValidator, TypeAdapter

import mic
from mic.integrations.pydantic import pydantic_schema

from .helpers import evaluation, row


def test_scorer_timeout_retains_output_and_earlier_scores(tmp_path):
    @mic.scorer(name="first")
    async def first(ctx):
        return 1

    @mic.scorer(name="slow")
    async def slow(ctx):
        await asyncio.Event().wait()
        return 1

    async def task(ctx, value):
        return value

    result = mic.run(
        evaluation([row()], task=task, scorers=[first, slow]), output=tmp_path, timeout=0.01
    )
    assert result.exit_code == 1
    case = result.cases[0]
    assert case["output"] == 1
    assert case["scores"] == [{"name": "first", "value": 1, "metadata": {}}]
    assert case["errors"][0]["phase"] == "scorer"
    assert case["errors"][0]["scorer"] == "slow"
    assert case["errors"][0]["type"] == "TimeoutError"
    assert result.manifest["scores"]["slow"]["unavailable_count"] == 1


def test_bare_output_dictionary_is_never_unpacked(tmp_path):
    value = {"output": "domain field", "metadata": {"source": "domain field"}}
    result = mic.run(evaluation([row(value)]), output=tmp_path)
    assert result.cases[0]["output"] == value
    assert "task_metadata" not in result.cases[0]


def test_trial_and_scorer_mutation_are_isolated(tmp_path):
    input_value = {"nested": [1]}
    expected_value = {"answer": [1]}
    observed = []

    def task(ctx, value):
        assert value == input_value
        assert ctx.expected == expected_value
        value["nested"].append(2)
        ctx.expected["answer"].append(2)
        ctx.metadata["label"] = "mutated"
        return {"answer": [1]}

    @mic.scorer(name="mutator")
    def mutate(ctx):
        assert ctx.input == input_value
        assert ctx.expected == expected_value
        ctx.input["nested"].append(3)
        ctx.output["answer"].append(3)
        ctx.expected["answer"].append(3)
        ctx.metadata["label"] = "scorer mutation"
        return 1

    @mic.scorer(name="observer")
    def observe(ctx):
        observed.append((ctx.input, ctx.output, ctx.expected, ctx.metadata))
        assert ctx.input == input_value
        assert ctx.output == expected_value
        assert ctx.expected == expected_value
        assert ctx.metadata == {"label": "original"}
        return 1

    result = mic.run(
        evaluation(
            [row(input_value, expected=expected_value, metadata={"label": "original"})],
            task=task,
            scorers=[mutate, observe],
            trials=3,
        ),
        output=tmp_path,
    )
    assert result.exit_code == 0
    assert len(observed) == 3
    assert input_value == {"nested": [1]}


def test_typed_metadata_is_projected_merged_and_hydrated(tmp_path):
    @dataclass
    class Metadata:
        label: str
        model: str | None = None

    @mic.scorer(name="metadata")
    def check(ctx):
        assert isinstance(ctx.metadata, Metadata)
        assert ctx.metadata.label == "task"
        assert ctx.metadata.model == "fixture"
        return 1

    schema = mic.case_schema(input=int, expected=int, metadata=Metadata)
    spec = evaluation(
        [row(metadata={"label": "row"})],
        schema=schema,
        output=int,
        task=lambda *_: mic.TaskResult(1, {"label": "task", "model": "fixture"}),
        scorers=[check],
    )
    result = mic.run(spec, output=tmp_path)
    assert result.exit_code == 0
    assert result.cases[0]["metadata"]["label"] == "row"
    assert result.cases[0]["task_metadata"]["label"] == "task"


@pytest.mark.parametrize("phase", ["task", "schema", "scorer"])
def test_case_failure_does_not_stop_other_rows(tmp_path, phase):
    def task(_ctx, value):
        if value == 1 and phase == "task":
            raise RuntimeError("expected fixture failure")
        return "wrong type" if value == 1 and phase == "schema" else value

    @mic.scorer(name="check")
    def score(ctx):
        if ctx.input == 1 and phase == "scorer":
            raise RuntimeError("expected scorer failure")
        return 1

    result = mic.run(
        evaluation([row(), row(2, id="b")], task=task, scorers=[score], output=int), output=tmp_path
    )
    assert result.exit_code == 1
    assert result.manifest["counts"]["completed"] == 1
    assert result.manifest["failures"][0]["phase"] == phase
    assert result.cases[1]["output"] == 2
    assert result.manifest["scores"]["check"]["unavailable_count"] == 1


def test_successful_scores_survive_a_later_scorer_failure(tmp_path):
    @mic.scorer(name="first")
    def first(ctx):
        return 0.8

    @mic.scorer(name="later")
    def later(ctx):
        return float("nan")

    result = mic.run(evaluation([row()], scorers=[first, later]), output=tmp_path)
    assert result.cases[0]["output"] == 1
    assert result.cases[0]["scores"] == [{"name": "first", "value": 0.8, "metadata": {}}]
    assert result.manifest["scores"]["first"]["mean"] == 0.8
    assert result.manifest["scores"]["later"]["unavailable_count"] == 1
    assert result.exit_code == 1


def test_dataset_transform_runs_once_before_trials_and_scorers(tmp_path):
    calls = []

    def record(value):
        calls.append(value)
        return value

    schema = mic.case_schema(
        input=pydantic_schema(TypeAdapter(Annotated[int, AfterValidator(record)])), expected=int
    )
    result = mic.run(evaluation([row()], schema=schema, output=int, trials=3), output=tmp_path)
    assert result.exit_code == 0
    assert calls == [1]


def test_unlabeled_and_present_null_remain_distinct_on_disk(tmp_path):
    seen = []

    @mic.scorer(name="optional", requires_expected=False)
    def scorer(ctx):
        seen.append(isinstance(ctx.expected, mic.Missing))
        return None

    schema = mic.case_schema(input=int, expected=mic.schema(int | None), expected_policy="optional")
    result = mic.run(
        evaluation(
            [{"id": "missing", "input": 1}, {"id": "null", "input": 2, "expected": None}],
            schema=schema,
            scorers=[scorer],
        ),
        output=tmp_path,
    )
    assert result.exit_code == 0
    assert sorted(seen) == [False, True]
    rows = [
        json.loads(line)
        for line in (tmp_path / "dataset.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert "expected" not in rows[0]
    assert rows[1]["expected"] is None
