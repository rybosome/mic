"""User-level proof for complex stdlib schemas through datasets, runner and reports."""

import json
from dataclasses import dataclass, replace

import pytest

import mic
from examples.structured import (
    Decision,
    Message,
    Metadata,
    Ticket,
    case_types,
    classify,
    classify_file,
    tickets,
)
from mic._runtime.materialization import load_dataset


def test_native_and_file_complex_cases_match_with_validated_output_and_metadata(tmp_path):
    memory = mic.run(classify, trials=3, output=tmp_path / "memory")
    file = mic.run(classify_file, trials=3, output=tmp_path / "file")
    assert memory.exit_code == file.exit_code == 0
    assert memory.manifest["dataset"]["digest"] == file.manifest["dataset"]["digest"]
    assert memory.manifest["counts"]["completed"] == file.manifest["counts"]["completed"] == 6
    assert memory.manifest["scores"]["label"]["mean"] == 1
    assert file.manifest["scores"]["evidence"]["mean"] == 1
    for row in memory.cases:
        assert row["output"]["evidence"] == {"messages": [0]}
        assert row["output"]["owner"] is None
        assert row["metadata"] == {"segment": "demo", "model": None}
        assert row["task_metadata"] == {"model": "rules"}
    manifest = json.loads((tmp_path / "file" / "run.json").read_text(encoding="utf-8"))
    assert manifest["dataset"]["schema"]["input"]
    assert "messages" in (tmp_path / "file" / "report.html").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_every_nested_consumer_gets_dataclass_instances():
    snapshot = await load_dataset(tickets)
    case = snapshot.cases[0]
    assert isinstance(case.input, Ticket)
    assert isinstance(case.input.messages[0], Message)
    assert isinstance(case.input.history["support"][0], Message)
    assert isinstance(case.expected, Decision)
    assert isinstance(case.metadata, Metadata)


def test_nested_dataclass_scorer_mutation_does_not_cross_trials_or_scorers(tmp_path):
    seen = []

    @mic.scorer(name="mutating")
    def mutating(ctx: mic.ScoreContext[Ticket, Decision, Decision, Metadata]) -> int:
        assert ctx.input.messages[0].text
        ctx.input.messages.clear()
        ctx.require_expected().evidence["messages"].append(99)
        ctx.output.evidence["messages"].append(88)
        return 1

    @mic.scorer(name="pristine")
    def pristine(ctx: mic.ScoreContext[Ticket, Decision, Decision, Metadata]) -> int:
        seen.append(ctx.case_id)
        assert len(ctx.input.messages) == 1
        assert ctx.require_expected().evidence == {"messages": [0]}
        assert ctx.output.evidence == {"messages": [0]}
        return 1

    spec = replace(classify, scorers=(mutating, pristine))
    result = mic.run(spec, trials=3, output=tmp_path / "isolation")
    assert result.exit_code == 0
    assert len(seen) == 6


def test_bad_nested_input_has_field_path_and_launches_zero_tasks(tmp_path):
    calls = []

    @mic.dataset(name="broken", schema=case_types)
    def broken():
        return [
            mic.RawCase(
                id="bad",
                input={"messages": [{"role": "user", "text": 17}]},
                expected=Decision("bug"),
            )
        ]

    @mic.eval(name="broken", dataset=broken, output=Decision, scorers=[])
    def task(ctx: mic.TaskContext[Decision, Metadata], value: Ticket) -> Decision:
        calls.append(value)
        return Decision("bug")

    with pytest.raises(mic.DatasetError, match=r"messages.*0.*text"):
        mic.run(task, output=tmp_path / "bad-input")
    assert calls == []


def test_bad_nested_output_is_a_schema_failure_before_scoring(tmp_path):
    @mic.eval(name="bad-output", dataset=tickets, output=Decision, scorers=[])
    def task(ctx: mic.TaskContext[Decision, Metadata], value: Ticket) -> Decision:
        decision = Decision("bug")
        decision.evidence["messages"] = ["wrong"]  # Deliberate external-output type violation.
        return decision

    result = mic.run(task, output=tmp_path / "bad-output")
    assert result.exit_code == 1
    assert result.manifest["counts"]["failed"] == 2
    for case in result.cases:
        assert case["errors"][0]["phase"] == "schema"
        assert "evidence" in case["errors"][0]["message"]


def test_a_custom_schema_adapter_can_add_domain_constraints(tmp_path):
    @dataclass
    class Count:
        value: int

    native = mic.schema(Count)

    class PositiveCount:
        def validate(self, value: object, *, strict: bool = True) -> Count:
            result = native.validate(value, strict=strict)
            if result.value < 0:
                raise ValueError("$.value: must be nonnegative")
            return result

        def dump(self, value: Count) -> object:
            return native.dump(value)

        def json_schema(self) -> dict[str, object]:
            return native.json_schema()

    @mic.dataset(name="custom", schema=mic.case_schema(input=PositiveCount(), expected=int))
    def data():
        return [mic.RawCase(input={"value": -1}, expected=0)]

    @mic.eval(name="custom", dataset=data, output=int, scorers=[])
    def task(ctx: mic.TaskContext[int, mic.JsonObject], count: Count) -> int:
        return count.value

    with pytest.raises(mic.DatasetError, match="nonnegative"):
        mic.run(task, output=tmp_path / "custom")
