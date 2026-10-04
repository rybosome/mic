"""Explicit formats, wrapped exports, and bounded JSON document traversal."""

import hashlib
import io
import json
import tracemalloc
from pathlib import Path

import pytest

import mic
from mic._runtime.datasets import DatasetReader
from mic._runtime.sources import open_source
from mic.providers import JSONFileHandle, JSONLFileHandle
from mic.sources import RecordError


async def read_rows(source, *, row_count=None):
    async with open_source(source, limits=mic.ReadLimits(row_count=row_count)) as read:
        return [row async for row in read.rows()], read.provenance, read.exhausted


@pytest.mark.parametrize("key", ["items", "", "data.items", 'é\\"items'])
@pytest.mark.parametrize("first", [True, False])
async def test_wrapped_arrays_preserve_rows_and_provenance(tmp_path, key, first):
    rows = [{"input": {"text": 'é ] \\"', "nested": [[1, 2]]}, "expected": None}, {"input": 2}]
    metadata = {"version": 1, "info": {"flags": [True, False, None, -1.25e10]}}
    document = {key: rows, **metadata} if first else {**metadata, key: rows}
    data = json.dumps(document, ensure_ascii=False).encode()
    path = tmp_path / "export.anything"
    path.write_bytes(data)
    actual, provenance, exhausted = await read_rows(JSONFileHandle(path, records_key=key))
    assert [{k: v for k, v in row.items() if k != "provenance"} for row in actual] == rows
    assert actual[1]["provenance"] == {"file": str(path), "source_row": 2}
    assert provenance == {
        "provider": "file",
        "path": str(path),
        "format": "json",
        "records_key": key,
        "raw_bytes": len(data),
        "raw_prefix_sha256": hashlib.sha256(data).hexdigest(),
        "raw_sha256": hashlib.sha256(data).hexdigest(),
        "read_complete": True,
    }
    assert exhausted


@pytest.mark.parametrize(
    "source_type,data", [(JSONFileHandle, '[{"input":1}]'), (JSONLFileHandle, '{"input":1}\n')]
)
async def test_class_controls_format_and_missing_file_errors(tmp_path, source_type, data):
    path = tmp_path / "no-extension"
    source = source_type(path)
    with pytest.raises(mic.DatasetError, match="Could not read dataset file"):
        await read_rows(source)
    path.write_text(data)
    assert (await read_rows(source))[0][0]["input"] == 1


@pytest.mark.parametrize("data", ['{"items":[]}', '{"before":[],"items":[],"after":{}}'])
async def test_empty_selected_array(tmp_path, data):
    path = tmp_path / "empty.json"
    path.write_text(data)
    rows, provenance, exhausted = await read_rows(JSONFileHandle(path, records_key="items"))
    assert rows == [] and exhausted and provenance["read_complete"]


@pytest.mark.parametrize(
    "data",
    [
        "{}",
        "[]",
        '{"items":{}}',
        '{"items":null}',
        '{"items":[],"items":[]}',
        '{"x":1,"x":2,"items":[]}',
        '{"x":{"a":1,"a":2},"items":[]}',
        '{"items":[],}',
        '{"items":[]',
        '{"items":[]} true',
        '{"items":[{},]}',
        '{"items":[{]}',
        '{"items":[],"x":NaN}',
        '{"items":[],"x":1e999}',
        '{"items":[],"x":[1,]}',
        '{"items":[],"x":"unterminated}',
        '{"items":[],"x":true false}',
        '{"items":[],"x":}',
        '{"items":[],"x":{"a" 1}}',
        '{"items":[],"x":{"a":1,}}',
        '{"items":[],"x":[1 2]}',
    ],
)
async def test_invalid_wrapper_or_framing_aborts(tmp_path, data):
    path = tmp_path / "bad.json"
    path.write_text(data)
    with pytest.raises(mic.DatasetError):
        await read_rows(JSONFileHandle(path, records_key="items"))


async def test_malformed_but_framed_array_element_is_a_record_error(tmp_path):
    path = tmp_path / "bad-row.json"
    path.write_text('{"items":[[,],{"input":1}]}')
    rows, _, exhausted = await read_rows(JSONFileHandle(path, records_key="items"))
    assert isinstance(rows[0], RecordError)
    assert rows[1]["input"] == 1 and exhausted


async def test_wrapper_errors_do_not_expose_source_tokens(tmp_path):
    path = tmp_path / "secret.json"
    for data in (b'{"items":[],"private":1e999123456789}', b'{"items":[],"private":"\xff"}'):
        path.write_bytes(data)
        with pytest.raises(mic.DatasetError) as error:
            await read_rows(JSONFileHandle(path, records_key="items"))
        assert str(error.value) == "Invalid JSON wrapper value"
        assert error.value.__suppress_context__


async def test_selected_prefix_does_not_parse_suffix_and_closes(tmp_path, monkeypatch):
    path = tmp_path / "prefix.json"
    data = b'{"items":[{"input":1},' + b" " * 20_000 + b"BROKEN"
    stream = io.BytesIO(data)
    monkeypatch.setattr(Path, "open", lambda *a, **kw: stream)
    rows, provenance, exhausted = await read_rows(
        JSONFileHandle(path, records_key="items"), row_count=1
    )
    assert rows[0]["input"] == 1
    assert stream.closed and not exhausted
    assert not provenance["read_complete"] and "raw_sha256" not in provenance
    assert provenance["raw_bytes"] < len(data)
    assert (
        provenance["raw_prefix_sha256"]
        == hashlib.sha256(data[: provenance["raw_bytes"]]).hexdigest()
    )


async def test_wrapped_rows_use_existing_mapping_and_skip_policy(tmp_path):
    path = tmp_path / "custom.json"
    path.write_text(
        '{"items":[{"question":"x","answer":null},{bad},42,{"question":"y","answer":"z"}]}'
    )
    source = JSONFileHandle(path, records_key="items")
    raw, _, _ = await read_rows(source)
    assert isinstance(raw[1], RecordError) and isinstance(raw[2], RecordError)

    def mapper(row):
        return mic.RawCase(
            input=row["question"], expected=row["answer"], provenance=row["provenance"]
        )

    dataset = mic.dataset(input=str, expected=str | None, map_row=mapper)(lambda: source)
    reader = DatasetReader(dataset, "test", mic.ReadLimits(), on_invalid="skip")
    items = [item async for item in reader.rows()]
    assert len(items) == 4
    assert items[0][1].input == "x" and items[0][1].expected is None
    assert items[3][1].input == "y"
    assert items[3][1].provenance["source_row"] == 4
    assert reader.accepted == 2 and reader.rejected == 2 and reader.exhausted
    with pytest.raises(mic.DatasetError):
        await mic.ainspect_dataset(dataset)


async def test_chunk_boundaries_and_large_ignored_array(tmp_path):
    path = tmp_path / "large.json"
    # Each ignored scalar fits in memory; the 2 MB array must not be retained.
    with path.open("w") as file:
        file.write('{"ignored":[')
        for index in range(2000):
            file.write(("," if index else "") + json.dumps("x" * 1000))
        file.write('],"items":[{"input":"é\\"' + "y" * 20_000 + '"}]}')
    tracemalloc.start()
    try:
        rows, provenance, exhausted = await read_rows(JSONFileHandle(path, records_key="items"))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert rows[0]["input"] == 'é"' + "y" * 20_000
    assert exhausted and provenance["raw_bytes"] == path.stat().st_size
    assert peak < 1_000_000


async def test_wrapper_nesting_limit_and_failure_cleanup(tmp_path, monkeypatch):
    stream = io.BytesIO(b'{"items":[],"x":' + b"[" * 130 + b"0" + b"]" * 130 + b"}")
    monkeypatch.setattr(Path, "open", lambda *a, **kw: stream)
    with pytest.raises(mic.DatasetError, match="nesting"):
        await read_rows(JSONFileHandle(tmp_path / "deep.json", records_key="items"))
    assert stream.closed


def test_handles_are_passive_and_selector_is_keyword_only(tmp_path):
    assert JSONFileHandle(tmp_path / "missing.json", records_key="items").records_key == "items"
    assert JSONLFileHandle(tmp_path / "missing.jsonl").path == tmp_path / "missing.jsonl"
    with pytest.raises(TypeError):
        JSONFileHandle("file", "items")
    with pytest.raises(mic.ConfigurationError):
        JSONFileHandle("file", records_key=1)


@pytest.mark.parametrize("depth", [128, 129])
@pytest.mark.parametrize("value", ["0", ""])
async def test_wrapper_nesting_boundary(tmp_path, depth, value):
    path = tmp_path / "depth.json"
    path.write_text('{"items":[],"x":' + "[" * depth + value + "]" * depth + "}")
    source = JSONFileHandle(path, records_key="items")
    if depth == 128:
        assert (await read_rows(source))[0] == []
    else:
        with pytest.raises(mic.DatasetError, match="nesting"):
            await read_rows(source)
