"""Independent checks at the source-to-validated-snapshot boundary."""

import asyncio
import json
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, TypedDict

import httpx
import pytest
from pydantic import BaseModel, Field

from mic.datasets import legacy_json_columns, load_dataset
from mic.decorators import case_schema, dataset
from mic.errors import ConfigurationError, DatasetError
from mic.integrations.pydantic import pydantic_schema
from mic.models import MISSING, RawCase, ReadLimits
from mic.providers.braintrust import BraintrustHandle, BraintrustLoader
from mic.resolver import Resolver


def definition(source, *, input=str, expected=str, expected_policy="required", map_row=None):
    return dataset(
        name="review",
        schema=case_schema(input=input, expected=expected, expected_policy=expected_policy),
        map_row=map_row,
    )(lambda: source)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["prefix", "validation"])
async def test_custom_read_iterator_finally_runs_before_loader_context_exit(stop):
    events = []

    @dataclass(frozen=True)
    class Handle:
        provider: str = "review"

    class Read:
        provenance = {"provider": "review"}

        async def rows(self):
            try:
                yield {"input": "valid" if stop == "prefix" else 123, "expected": "yes"}
                yield {"input": "next", "expected": "yes"}
            finally:
                events.append("iterator closed")

    class Loader:
        @asynccontextmanager
        async def open(self, handle, *, limits) -> AsyncGenerator[Read]:
            try:
                yield Read()
            finally:
                events.append("loader closed")

    resolver = Resolver()
    resolver.register(Handle, Loader())
    if stop == "prefix":
        await load_dataset(definition(Handle()), resolver=resolver, limit=1)
    else:
        with pytest.raises(DatasetError):
            await load_dataset(definition(Handle()), resolver=resolver)
    assert events == ["iterator closed", "loader closed"]


@pytest.mark.asyncio
async def test_braintrust_prefix_closes_stream_without_closing_injected_client():
    class Body(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            first = b'{"id":"a","input":"x","expected":"x"}\n'
            yield first + b"\n" * (8192 - len(first))
            await asyncio.sleep(2)

        async def aclose(self):
            self.closed = True

    body = Body()
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, stream=body, headers={"x-bt-cursor": "cursor"})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        resolver = Resolver()
        resolver.register(BraintrustHandle, BraintrustLoader(client=client, api_key="fixture"))
        await load_dataset(definition(BraintrustHandle("id", "pinned")), resolver=resolver, limit=1)
        assert body.closed
        assert not client.is_closed


@dataclass
class PositiveInput:
    count: Annotated[int, Field(ge=0)]


class PositiveModel(BaseModel):
    count: Annotated[int, Field(ge=0)]


class AliasedModel(BaseModel):
    value: int = Field(alias="external_value")


@pytest.mark.asyncio
async def test_valid_model_alias_supports_native_and_saved_snapshot_roundtrip():
    native = AliasedModel(external_value=2)
    snapshot = await load_dataset(
        definition(
            [{"input": native, "expected": "x"}],
            input=AliasedModel,
        )
    )
    reloaded = await load_dataset(definition(snapshot.rows, input=AliasedModel))
    assert snapshot.cases[0].input.value == reloaded.cases[0].input.value == 2
    assert snapshot.summary["digest"] == reloaded.summary["digest"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value,annotation",
    [
        (PositiveInput(-1), pydantic_schema(PositiveInput)),
        (PositiveModel.model_construct(count=-1), PositiveModel),
    ],
)
async def test_native_instances_cannot_bypass_field_validation(value, annotation):
    with pytest.raises(DatasetError, match="greater than or equal"):
        await load_dataset(definition([{"input": value, "expected": "x"}], input=annotation))


@dataclass
class MutableInput:
    values: list[int]


@pytest.mark.asyncio
async def test_reused_native_instance_cannot_change_already_snapshotted_cases():
    reused = MutableInput([1])

    def rows():
        yield {"id": "one", "input": reused, "expected": "x"}
        reused.values.append(2)
        yield {"id": "two", "input": reused, "expected": "x"}
        reused.values.append(3)

    snapshot = await load_dataset(definition(rows(), input=MutableInput))
    assert snapshot.rows[0]["input"] == {"values": [1]}
    assert snapshot.rows[1]["input"] == {"values": [1, 2]}
    assert snapshot.cases[0].input.values == [1]
    assert snapshot.cases[1].input.values == [1, 2]


class StructuredInput(TypedDict):
    value: int
    tags: list[str]


@pytest.mark.asyncio
async def test_dataclass_and_typed_dict_hydrate_from_json_without_coercing_primitives():
    dataclass_snapshot = await load_dataset(
        definition(
            [{"input": {"values": [1, 2]}, "expected": "x"}],
            input=MutableInput,
        )
    )
    assert dataclass_snapshot.cases[0].input == MutableInput([1, 2])
    typed_snapshot = await load_dataset(
        definition(
            [{"input": {"value": 2, "tags": ["a"]}, "expected": "x"}],
            input=StructuredInput,
        )
    )
    assert typed_snapshot.cases[0].input == {"value": 2, "tags": ["a"]}
    with pytest.raises(DatasetError, match="integer|int"):
        await load_dataset(
            definition(
                [{"input": {"value": "2", "tags": ["a"]}, "expected": "x"}],
                input=StructuredInput,
            )
        )


@pytest.mark.asyncio
async def test_custom_mapper_and_legacy_columns_preserve_absent_null_and_provenance():
    source = [
        {"id": "missing", "input_json": json.dumps([1]), "provenance": {"row": 1}},
        {
            "id": "null",
            "input_json": json.dumps([2]),
            "expected_json": "null",
            "provenance": {"row": 2},
        },
    ]
    snapshot = await load_dataset(
        definition(
            source,
            input=list[int],
            expected=str | None,
            expected_policy="optional",
            map_row=legacy_json_columns,
        )
    )
    assert snapshot.cases[0].expected == MISSING
    assert snapshot.cases[1].expected is None
    assert "expected" not in snapshot.rows[0]
    assert snapshot.rows[1]["expected"] is None
    assert snapshot.cases[0].provenance == {"row": 1}

    def mapping(row):
        return RawCase(input=row["x"], expected=row["y"], id="mapped", provenance={"manual": True})

    mapped = await load_dataset(definition([{"x": "input", "y": "expected"}], map_row=mapping))
    assert mapped.cases[0].provenance == {"manual": True}
    with pytest.raises(DatasetError, match="map_row must return RawCase"):
        await load_dataset(definition([{"input": "x", "expected": "y"}], map_row=lambda r: r))


@pytest.mark.asyncio
async def test_empty_infinite_and_byte_limited_sources():
    empty = await load_dataset(definition([]))
    assert empty.rows == []
    assert empty.summary["rows"] == 0
    assert (
        empty.summary["digest"]
        == "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )

    def forever():
        while True:
            yield {"input": "x", "expected": "x"}

    with pytest.raises(DatasetError, match="max_rows=3"):
        await load_dataset(definition(forever()), limits=ReadLimits(max_rows=3))
    with pytest.raises(DatasetError, match="max_record_bytes=10"):
        await load_dataset(
            definition([{"input": "x" * 100, "expected": "x"}]),
            limits=ReadLimits(max_record_bytes=10),
        )
    with pytest.raises(DatasetError, match="max_bytes=20"):
        await load_dataset(
            definition([{"input": "x" * 100, "expected": "x"}]), limits=ReadLimits(max_bytes=20)
        )


@pytest.mark.asyncio
async def test_async_timeout_closes_source_generator():
    closed = []

    async def slow():
        try:
            yield {"input": "x", "expected": "x"}
            await asyncio.sleep(2)
            yield {"input": "y", "expected": "y"}
        finally:
            closed.append(True)

    with pytest.raises(DatasetError, match="TimeoutError"):
        await load_dataset(definition(slow()), limits=ReadLimits(timeout_seconds=0.01))
    assert closed == [True]


@pytest.mark.asyncio
async def test_cancelled_sync_factory_cleans_up_its_late_returned_iterator():
    started = threading.Event()
    released = threading.Event()
    returned = threading.Event()
    sources = []

    class Source:
        closed = False

        def __iter__(self):
            return self

        def __next__(self):
            raise StopIteration

        def close(self):
            self.closed = True

    def factory():
        started.set()
        assert released.wait(2)
        source = Source()
        sources.append(source)
        returned.set()
        return source

    descriptor = dataset(name="late-factory", schema=case_schema(input=str, expected=str))(factory)
    task = asyncio.create_task(load_dataset(descriptor))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    released.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(returned.wait, 2)
    await asyncio.sleep(0)
    assert sources[0].closed


def test_resolver_unknown_and_duplicate_registration_are_explicit():
    resolver = Resolver()
    with pytest.raises(ConfigurationError, match="No dataset loader registered"):
        resolver.open(object(), limits=ReadLimits())
