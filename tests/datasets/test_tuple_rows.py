"""Tuple authoring shares normalization, occurrence identity, and stream ownership."""

from dataclasses import dataclass

import pytest
from pydantic import BaseModel

import mic
from mic._runtime.datasets import DatasetReader
from tests.datasets.helpers import collect_dataset


@dataclass
class NativeInput:
    values: list[int]


class ModelInput(BaseModel):
    values: list[int]


@pytest.mark.parametrize("input_type", [NativeInput, ModelInput])
async def test_tuple_rawcase_and_mapping_have_equal_values_digests_and_identity(input_type):
    value = input_type(values=[1, 2])
    snapshots = []
    for row in ((value, True), mic.RawCase(value, True), {"input": value, "expected": True}):
        dataset = mic.dataset(input=input_type, expected=bool)(lambda: [row, row])
        snapshots.append(await collect_dataset(dataset))
    for result in snapshots:
        assert result.rows == [{"input": {"values": [1, 2]}, "expected": True}] * 2
        assert [case.id for case in result.cases] == ["fixture-source:r0", "fixture-source:r1"]
        assert result.cases[0].input.values == [1, 2]
        assert result.summary["digest"] == snapshots[0].summary["digest"]


async def test_mixed_rows_preserve_explicit_labels_metadata_and_provenance():
    dataset = mic.dataset(input=list[int], expected=str)(
        lambda: [
            ([1], "a"),
            mic.RawCase(
                [2], "b", id="second", metadata={"group": 1}, provenance={"source": "manual"}
            ),
            {"input": [3], "expected": "c", "id": "third"},
        ]
    )
    result = await collect_dataset(dataset)
    assert result.rows == [
        {"input": [1], "expected": "a"},
        {
            "input": [2],
            "expected": "b",
            "label": "second",
            "metadata": {"group": 1},
            "provenance": {"source": "manual"},
        },
        {"input": [3], "expected": "c", "label": "third"},
    ]


async def test_tuple_missing_and_null_stay_distinct():
    dataset = mic.dataset(input=str, expected=str | None, expected_policy="optional")(
        lambda: [("missing", mic.MISSING), ("null", None)]
    )
    result = await collect_dataset(dataset)
    assert result.cases[0].expected is mic.MISSING
    assert "expected" not in result.rows[0]
    assert result.cases[1].expected is None
    assert result.rows[1]["expected"] is None
    reader = DatasetReader(dataset, "required-scorer", mic.ReadLimits(), require_expected=True)
    with pytest.raises(mic.DatasetError):
        _ = [row async for row in reader.rows()]
    assert reader.rejected == 1


@pytest.mark.parametrize(
    "row",
    [
        (),
        ("x",),
        ("x", "y", "z"),
        ["x", "y"],
        "xy",
        (1, "y"),
        ("x", 2),
        ("x", None),
        ("x", mic.MISSING),
    ],
)
async def test_invalid_tuple_shapes_and_values_obey_abort_and_skip(row):
    dataset = mic.dataset(input=str, expected=str)(lambda: [row, ("valid", "yes")])
    with pytest.raises(mic.DatasetError):
        await collect_dataset(dataset)
    reader = DatasetReader(dataset, "test", mic.ReadLimits(), on_invalid="skip")
    items = [item async for item in reader.rows()]
    assert len(items[0]) == 2
    assert items[1][1].input == "valid"
    assert items[1][1].id == "test:r1"
    assert reader.accepted == reader.rejected == 1
    assert reader.exhausted


async def test_custom_mapper_receives_original_tuple_and_can_use_other_lengths():
    row = ("input", "answer", "label")
    received = []

    def mapper(value):
        received.append(value)
        return mic.RawCase(value[0], value[1], id=value[2])

    dataset = mic.dataset(input=str, expected=str, map_row=mapper)(lambda: [row])
    result = await collect_dataset(dataset)
    assert received[0] is row
    assert result.rows == [{"input": "input", "expected": "answer", "label": "label"}]
    invalid = mic.dataset(input=str, expected=str, map_row=lambda value: value)(
        lambda: [("x", "y")]
    )
    with pytest.raises(mic.DatasetError):
        await collect_dataset(invalid)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stop", ["exhaustion", "prefix", "invalid"])
async def test_tuple_generators_are_fresh_bounded_and_closed(asynchronous, stop):
    closed = []
    consumed = []

    def sync_rows():
        try:
            for index in range(3):
                consumed.append(index)
                yield (index, None if stop == "invalid" else index * 2)
        finally:
            closed.append(True)

    async def async_rows():
        try:
            for index in range(3):
                consumed.append(index)
                yield (index, None if stop == "invalid" else index * 2)
        finally:
            closed.append(True)

    dataset = mic.dataset(input=int, expected=int)(async_rows if asynchronous else sync_rows)
    for _ in range(2):
        if stop == "invalid":
            with pytest.raises(mic.DatasetError):
                await collect_dataset(dataset)
        else:
            result = await collect_dataset(
                dataset, limits=mic.ReadLimits(row_count=1 if stop == "prefix" else None)
            )
            assert result.summary["exhausted"] == (stop == "exhaustion")
            assert len(result.rows) == (1 if stop == "prefix" else 3)
    assert consumed == ([0, 1, 2] * 2 if stop == "exhaustion" else [0, 0])
    assert closed == [True, True]


async def test_a_single_pair_is_not_a_whole_dataset():
    dataset = mic.dataset(input=str, expected=str)(lambda: ("input", "expected"))
    with pytest.raises(mic.DatasetError):
        await collect_dataset(dataset)
