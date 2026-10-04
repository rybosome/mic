"""Built-ins and external providers share one lifecycle, without registration."""

import ast
import asyncio
import threading
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

import mic
from examples.yaml_source import YamlDocuments
from mic._runtime.sources import open_source
from mic.providers import BigQueryHandle, BraintrustHandle, JSONFileHandle, JSONLFileHandle


@pytest.mark.parametrize(
    "source", [BigQueryHandle, BraintrustHandle, JSONFileHandle, JSONLFileHandle, YamlDocuments]
)
def test_sources_implement_only_public_contract(source) -> None:
    assert issubclass(source, mic.DatasetSource)
    path = Path(__import__(source.__module__, fromlist=["__file__"]).__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assert not any(
        isinstance(node, ast.ImportFrom) and "_runtime" in (node.module or "")
        for node in ast.walk(tree)
    )


async def test_sync_source_is_lazy_reusable_and_closes_on_the_same_worker() -> None:
    events = []

    @dataclass(frozen=True)
    class Source(mic.DatasetSource):
        def read(self, ctx: mic.ReadContext) -> Iterator[object]:
            events.append(("open", threading.get_ident()))
            ctx.set_provenance(provider="external")
            try:
                for value in range(100):
                    events.append((value, threading.get_ident()))
                    yield {"input": value}
            finally:
                events.append(("close", threading.get_ident()))

    source = Source()
    assert events == []
    for _ in range(2):
        async with open_source(source, limits=mic.ReadLimits()) as read:
            iterator = read.rows()
            assert await anext(iterator) == {"input": 0}
            await asyncio.sleep(0)
            assert events[-1][0] == 0
        assert [name for name, _ in events[-3:]] == ["open", 0, "close"]
        assert len({worker for _, worker in events[-3:]}) == 1
        assert events[-1][1] != threading.get_ident()


async def test_async_source_closes_after_consumer_failure() -> None:
    closed = []

    class Source(mic.DatasetSource):
        async def read(self, ctx: mic.ReadContext) -> AsyncIterator[object]:
            try:
                yield mic.RawCase(1, 2)
                pytest.fail("read ahead of demand")
            finally:
                closed.append(True)

    with pytest.raises(RuntimeError, match="consumer"):
        async with open_source(Source(), limits=mic.ReadLimits()) as read:
            async for _ in read.rows():
                raise RuntimeError("consumer")
    assert closed == [True]


async def test_repeated_cancellation_joins_sync_read_before_closing() -> None:
    entered = threading.Event()
    release = threading.Event()
    events = []

    class Source(mic.DatasetSource):
        def read(self, ctx):
            try:
                entered.set()
                assert release.wait(5)
                events.append("read finished")
                yield 1
            finally:
                events.append("closed")

    async def consume():
        async with open_source(Source(), limits=mic.ReadLimits()) as read:
            return [row async for row in read.rows()]

    task = asyncio.create_task(consume())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        assert events == []
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == ["read finished", "closed"]


async def test_malformed_source_contract_and_row_caps_are_central() -> None:
    class BadSource(mic.DatasetSource):
        def read(self, ctx):
            return [1, 2]

    with pytest.raises(mic.ConfigurationError, match="return an iterator"):
        async with open_source(BadSource(), limits=mic.ReadLimits()) as read:
            _ = [row async for row in read.rows()]

    class Source(mic.DatasetSource):
        def read(self, ctx):
            yield from range(3)

    async with open_source(Source(), limits=mic.ReadLimits(row_count=2)) as read:
        assert [row async for row in read.rows()] == [0, 1]
        assert not read.exhausted


def test_context_isolates_and_bounds_provenance() -> None:
    ctx = mic.ReadContext(mic.ReadLimits())
    nested = {"values": [1]}
    ctx.set_provenance(nested=nested)
    nested["values"].append(2)
    copied = ctx.provenance
    copied["nested"]["values"].append(3)
    assert ctx.provenance == {"nested": {"values": [1]}}
    with pytest.raises(mic.DatasetError, match="65536"):
        ctx.set_provenance(oversized="x" * 65536)


async def test_yaml_source_hydrates_dataclasses_without_registration(tmp_path) -> None:
    @dataclass
    class Input:
        body: str

    path = tmp_path / "cases.yaml"
    path.write_text(
        "input: {body: Cannot sign in}\nexpected: bug\n---\n"
        "input: {body: Please add CSV}\nexpected: feature\n",
        encoding="utf-8",
    )

    @mic.dataset(input=Input, expected=str)
    def tickets():
        return YamlDocuments(path)

    result = await mic.ainspect_dataset(tickets)
    assert result["dataset"]["records_accepted"] == 2
    assert result["rows"][0]["input"] == {"body": "Cannot sign in"}
    assert result["dataset"]["exhausted"] is True
    assert result["dataset"]["provenance"] == {"provider": "yaml", "path": str(path)}


@pytest.mark.parametrize(
    "data",
    [
        "input: [broken",
        "input: 1\ninput: 2",
        "input: &a 1\nexpected: *a",
        "input: !!python/object:secret {}",
        "input: {1: value}",
        "input: " + "[" * 65 + "1" + "]" * 65,
        "input: [" + ",".join(["1"] * 10001) + "]",
    ],
)
async def test_yaml_parser_failures_are_fatal_and_do_not_quote_documents(tmp_path, data) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(data, encoding="utf-8")
    with pytest.raises(mic.DatasetError, match="Invalid or unsupported YAML") as error:
        async with open_source(YamlDocuments(path), limits=mic.ReadLimits()) as read:
            _ = [row async for row in read.rows()]
    assert data not in str(error.value)


@pytest.mark.parametrize("data", ["{}", "[{},]", "[,{}]", "[{}", "[{}] {}", "[}]"])
async def test_invalid_array_framing_is_fatal(tmp_path, data) -> None:
    path = tmp_path / "bad.json"
    path.write_text(data, encoding="utf-8")
    with pytest.raises(mic.DatasetError):
        async with open_source(JSONFileHandle(path), limits=mic.ReadLimits()) as read:
            _ = [row async for row in read.rows()]


async def test_array_reader_handles_nested_escaped_and_utf8_records_incrementally(tmp_path) -> None:
    import json

    path = tmp_path / "cases.json"
    rows = [{"input": {"text": 'é ] \\"', "nested": [[1, 2]]}}] * 1000
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    async with open_source(JSONFileHandle(path), limits=mic.ReadLimits()) as read:
        iterator = read.rows()
        first = await anext(iterator)
        assert first["input"] == rows[0]["input"]
        assert read.provenance["raw_bytes"] < path.stat().st_size
        assert read.provenance["read_complete"] is False
        rest = [row async for row in iterator]
        assert len(rest) == 999
        assert read.provenance["read_complete"] is True
