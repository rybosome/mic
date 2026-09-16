import asyncio
from pathlib import Path

import pytest

from mic.errors import ConfigurationError, DatasetError
from mic.models import ReadLimits
from mic.providers.files import FileHandle, FileLoader
from mic.providers.memory import MemoryLoader


async def file_rows(path: Path, limits: ReadLimits = ReadLimits()):
    async with FileLoader().open(FileHandle(path), limits=limits) as read:
        rows = [row async for row in read.rows()]
        return rows, read.provenance


@pytest.mark.asyncio
async def test_jsonl_preserves_raw_missing_null_and_physical_lines(tmp_path):
    path = tmp_path / "cases.jsonl"
    path.write_text('\n{"input": "x", "expected": null}\n\n{"input": 3}\n', encoding="utf-8")
    rows, provenance = await file_rows(path)
    assert rows[0]["expected"] is None
    assert "expected" not in rows[1]
    assert rows[0]["provenance"]["line"] == 2
    assert rows[1]["provenance"]["line"] == 4
    assert rows[1]["provenance"]["source_row"] == 2
    assert len(provenance["raw_sha256"]) == 64
    assert provenance["raw_bytes"] == path.stat().st_size


@pytest.mark.asyncio
async def test_json_array_and_custom_columns(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text('[{"question": {"a": [1, 2]}, "answer": "yes"}]', encoding="utf-8")
    rows, _ = await file_rows(path)
    assert rows[0]["question"] == {"a": [1, 2]}
    assert rows[0]["answer"] == "yes"
    assert rows[0]["provenance"]["source_row"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content,match",
    [
        ('{"input":1}\n{bad}\n', "line 2"),
        ('{"input":NaN}\n', "nonfinite"),
        ('{"input":1e999}\n', "nonfinite"),
        ('{"input":1,"input":2}\n', "duplicate object key"),
        ("[1,2]\n", "case object"),
        ('{"input":1,"provenance":3}\n', "provenance must be"),
    ],
)
async def test_jsonl_invalid(tmp_path, content, match):
    path = tmp_path / "bad.jsonl"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(DatasetError, match=match):
        await file_rows(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["json", "jsonl"])
async def test_file_row_caps_fail_instead_of_truncate(tmp_path, suffix):
    path = tmp_path / f"rows.{suffix}"
    path.write_text(
        '[{"input":1},{"input":2}]' if suffix == "json" else '{"input":1}\n{"input":2}\n',
        encoding="utf-8",
    )
    with pytest.raises(DatasetError, match="max_rows=1"):
        await file_rows(path, ReadLimits(max_rows=1))


@pytest.mark.asyncio
async def test_file_record_and_raw_byte_caps(tmp_path):
    path = tmp_path / "rows.jsonl"
    path.write_text('{"input":"' + "x" * 100 + '"}\n', encoding="utf-8")
    with pytest.raises(DatasetError, match="max_record_bytes=20"):
        await file_rows(path, ReadLimits(max_record_bytes=20))
    with pytest.raises(DatasetError, match="max_bytes=30"):
        await file_rows(path, ReadLimits(max_bytes=30))
    path = tmp_path / "rows.json"
    path.write_text('[{"input":"' + "x" * 100 + '"}]', encoding="utf-8")
    with pytest.raises(DatasetError, match="max_bytes=30"):
        await file_rows(path, ReadLimits(max_bytes=30))


@pytest.mark.asyncio
async def test_file_closes_when_consumer_stops_or_raises(tmp_path):
    path = tmp_path / "cases.jsonl"
    path.write_text('{"input":1}\n{"input":2}', encoding="utf-8")
    with pytest.raises(RuntimeError):
        async with FileLoader().open(FileHandle(path), limits=ReadLimits()) as read:
            async for _ in read.rows():
                break
            assert not read.stream.closed
            raise RuntimeError("consumer")
    assert read.stream.closed


@pytest.mark.asyncio
async def test_memory_sync_async_and_infinite_caps():
    async def async_source():
        yield {"input": 3}
        await asyncio.sleep(0)
        yield {"input": 4}

    for source in ([{"input": 3}, {"input": 4}], async_source()):
        async with MemoryLoader().open(source, limits=ReadLimits()) as read:
            assert [row async for row in read.rows()] == [{"input": 3}, {"input": 4}]
            with pytest.raises(DatasetError, match="only be iterated once"):
                _ = [row async for row in read.rows()]

    def forever():
        while True:
            yield {"input": 3}

    async with MemoryLoader().open(forever(), limits=ReadLimits(max_rows=2)) as read:
        with pytest.raises(DatasetError, match="max_rows=2"):
            _ = [row async for row in read.rows()]


@pytest.mark.asyncio
async def test_memory_rejects_string_or_dict_as_whole_source():
    for value in ("text", {"input": 1}):
        with pytest.raises(ConfigurationError, match="iterable of rows"):
            async with MemoryLoader().open(value, limits=ReadLimits()):
                pass


@pytest.mark.asyncio
async def test_memory_closes_generators_on_prefix_and_validation_failure():
    closed = []

    def sync_rows():
        try:
            yield {"input": 1}
            yield {"input": 2}
        finally:
            closed.append("sync")

    async def async_rows():
        try:
            yield {"input": 1}
            yield {"input": 2}
        finally:
            closed.append("async")

    for source in (sync_rows(), async_rows()):
        with pytest.raises(RuntimeError):
            async with MemoryLoader().open(source, limits=ReadLimits()) as read:
                async for _ in read.rows():
                    raise RuntimeError("consumer validation failure")
    assert closed == ["sync", "async"]


@pytest.mark.asyncio
async def test_jsonl_prefix_provenance_does_not_claim_full_file_digest(tmp_path):
    path = tmp_path / "cases.jsonl"
    path.write_text('{"input":1}\n{"input":2}\n', encoding="utf-8")
    async with FileLoader().open(FileHandle(path), limits=ReadLimits()) as read:
        async for _ in read.rows():
            break
    assert read.provenance["read_complete"] is False
    assert "raw_sha256" not in read.provenance
    assert len(read.provenance["raw_prefix_sha256"]) == 64
    assert read.provenance["raw_bytes"] < path.stat().st_size
