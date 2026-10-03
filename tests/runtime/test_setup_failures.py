"""Configuration and filesystem failures cannot masquerade as running evaluations."""

import json
from dataclasses import replace

import pytest

import mic

from .helpers import evaluation, row


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
        spec, dataset=replace(spec.dataset, factory=lambda: calls.append("source") or [row()])
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
    assert not (tmp_path / "run.json").exists()


def test_noncallable_scorer_fails_before_source_access(tmp_path):
    calls = []
    spec = evaluation([row()])
    spec = replace(
        spec,
        dataset=replace(spec.dataset, factory=lambda: calls.append("source") or [row()]),
        scorers=(replace(spec.scorers[0], function=None),),
    )
    with pytest.raises(mic.ConfigurationError, match="scorer.*callable"):
        mic.run(spec, output=tmp_path)
    assert calls == []
    assert not (tmp_path / "run.json").exists()


def test_run_artifacts_include_the_declared_output_schema(tmp_path):
    result = mic.run(evaluation([row()], output=int), output=tmp_path)
    assert result.exit_code == 0
    assert json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))["info"]["tasks"][
        "runtime"
    ]["output"] == {"type": "integer"}
