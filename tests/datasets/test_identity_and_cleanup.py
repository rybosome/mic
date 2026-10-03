"""Source identity and cleanup are independent of a provider's implementation."""

import asyncio
import json
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

import mic
from tests.datasets.helpers import collect_dataset


async def test_provider_digest_does_not_change_fallback_case_identity():
    @dataclass
    class Handle(mic.DatasetSource):
        provider: str = "custom"

        async def read(self, ctx):
            ctx.set_provenance(digest="physical-source-digest", provider="custom")
            yield {"input": 1, "expected": 2}

    schema = mic.case_schema(input=int, expected=int)

    @mic.dataset(name="custom", schema=schema)
    def custom():
        return Handle()

    @mic.dataset(name="memory", schema=schema)
    def memory():
        return [{"input": 1, "expected": 2}]

    provider = await collect_dataset(custom)
    local = await collect_dataset(memory)
    assert provider.rows == local.rows
    assert provider.summary["digest"] == local.summary["digest"]


@pytest.mark.parametrize("entrypoint", ["materializer", "run"])
async def test_repeated_cancel_drains_source_factory_and_its_close(tmp_path: Path, entrypoint: str):
    entered = asyncio.Event()
    closing = asyncio.Event()
    release_factory = threading.Event()
    release_close = threading.Event()
    loop = asyncio.get_running_loop()
    events = []

    class Source:
        def __iter__(self):
            return iter([])

        def close(self):
            loop.call_soon_threadsafe(closing.set)
            assert release_close.wait(2)
            events.append("closed")

    @mic.dataset(name="slow-source", schema=mic.case_schema(input=int, expected=int))
    def source():
        loop.call_soon_threadsafe(entered.set)
        assert release_factory.wait(2)
        events.append("returned")
        return Source()

    task_calls = []

    @mic.eval(name="cancel-during-setup", dataset=source, output=int, scorers=[])
    def identity(ctx, value):
        task_calls.append(value)
        return value

    pending = (
        collect_dataset(source)
        if entrypoint == "materializer"
        else mic.arun(identity, output=tmp_path)
    )
    task = asyncio.create_task(pending)
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done()
        release_factory.set()
        await asyncio.wait_for(closing.wait(), timeout=2)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done()
    finally:
        release_factory.set()
        release_close.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert events == ["returned", "closed"]
    assert task_calls == []
    if entrypoint == "run":
        manifest = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
        assert manifest["status"] == "cancelled"
        assert manifest["exit_code"] == 130
        assert manifest["summary"]["trials"]["planned"] == 0
        assert (tmp_path / "events.jsonl").is_file()
