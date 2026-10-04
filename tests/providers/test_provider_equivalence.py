"""The same normalized cases cross each adapter and the real resolver boundary."""

import json
from dataclasses import dataclass
from enum import Enum

import httpx
import pytest

from mic.decorators import case_schema, dataset
from mic.errors import DatasetError
from mic.models import ReadLimits
from mic.providers.bigquery import BigQueryHandle
from mic.providers.braintrust import BraintrustHandle
from mic.providers.files import JSONLFileHandle
from mic.sources import DatasetSource
from tests.datasets.helpers import collect_dataset

ROWS = [
    {"id": "case-001", "input": "nested text", "expected": "label", "metadata": {"team": "x"}},
    {"id": "case-002", "input": "nullable", "expected": None},
    {"id": "case-003", "input": "unlabeled"},
]


class QueryJob:
    job_id = "fixture"
    total_bytes_processed = 0
    total_bytes_billed = 0
    statement_type = "SELECT"
    cache_hit = False

    def __init__(self, rows):
        self.rows = rows

    def result(self, *, page_size, timeout):
        return iter(self.rows)

    def cancel(self):
        return True


class Client:
    def __init__(self, rows):
        self.rows = rows

    def query(self, query, *, job_config, location, timeout):
        return QueryJob(self.rows)

    def close(self):
        pass


def source_fixture(kind, rows, tmp_path):
    if kind == "memory":
        return rows
    if kind == "file":
        path = tmp_path / "fixture.jsonl"
        path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
        return JSONLFileHandle(path)
    if kind == "bigquery":
        return BigQueryHandle(
            "billing-project",
            "SELECT * FROM cases ORDER BY id",
            "US",
            client=Client(rows),
            config_factory=lambda h, d: d,
        )
    if kind == "braintrust":
        responses = iter(
            [
                httpx.Response(
                    200,
                    content="\n".join(json.dumps(row) for row in rows),
                    headers={"x-bt-cursor": "next"},
                ),
                httpx.Response(200, content=b""),
            ]
        )
        return BraintrustHandle(
            "dataset-id",
            "pinned-version",
            api_key="fixture-only",
            transport=httpx.MockTransport(lambda _: next(responses)),
        )
    raise AssertionError(kind)


@pytest.mark.asyncio
async def test_all_builtin_sources_have_identical_normalized_records(tmp_path):
    snapshots = []
    for kind in ("memory", "file", "bigquery", "braintrust"):
        source = source_fixture(kind, ROWS, tmp_path)
        descriptor = dataset(
            name="same",
            schema=case_schema(input=str, expected=str | None, expected_policy="optional"),
        )(lambda: source)
        snapshots.append(await collect_dataset(descriptor))
    assert all(logical(snapshot.rows) == logical(ROWS) for snapshot in snapshots)
    assert len({snapshot.summary["digest"] for snapshot in snapshots}) == 1
    assert snapshots[1].cases[0].provenance["line"] == 1
    assert snapshots[3].cases[0].provenance["record_id"] == "case-001"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "file", "bigquery", "braintrust"])
async def test_every_source_preserves_labels_but_generates_distinct_identities(kind, tmp_path):
    source = source_fixture(kind, [ROWS[0], ROWS[0]], tmp_path)
    descriptor = dataset(name="duplicate", schema=case_schema(input=str, expected=str))(
        lambda: source
    )
    result = await collect_dataset(descriptor)
    assert len({case.id for case in result.cases}) == 2
    assert result.rows[0]["label"] == result.rows[1]["label"]


@pytest.mark.asyncio
async def test_fifth_source_needs_no_registration_or_core_change():
    @dataclass(frozen=True)
    class FixtureHandle(DatasetSource):
        provider: str = "custom-fixture"

        def read(self, ctx):
            ctx.set_provenance(provider=self.provider)
            yield from ROWS

    descriptor = dataset(
        name="custom",
        schema=case_schema(input=str, expected=str | None, expected_policy="optional"),
    )(lambda: FixtureHandle())
    result = await collect_dataset(descriptor, limits=ReadLimits())
    assert logical(result.rows) == logical(ROWS)


class NativeMode(str, Enum):
    FAST = "fast"
    SLOW = "slow"


@dataclass(frozen=True)
class NativeEntry:
    label: str
    count: int


@dataclass
class NativePayload:
    entries: list[NativeEntry]
    tags: tuple[str, ...]
    mode: NativeMode
    optional: NativeEntry | None


@pytest.mark.asyncio
async def test_nested_native_dataclass_shapes_have_equal_digest_across_all_sources(tmp_path):
    rows = [
        {
            "id": "nested",
            "input": {
                "entries": [{"label": "one", "count": 2}],
                "tags": ["a", "b"],
                "mode": "fast",
                "optional": None,
            },
            "expected": {"count": 2},
            "metadata": {"nested": {"trace": [1, "a"]}},
        }
    ]
    snapshots = []
    for kind in ("memory", "file", "bigquery", "braintrust"):
        source = source_fixture(kind, rows, tmp_path)
        descriptor = dataset(
            name="nested",
            schema=case_schema(
                input=NativePayload,
                expected=dict[str, int],
            ),
        )(lambda: source)
        snapshot = await collect_dataset(descriptor)
        assert snapshot.cases[0].input == NativePayload(
            [NativeEntry("one", 2)],
            ("a", "b"),
            NativeMode.FAST,
            None,
        )
        assert logical(snapshot.rows) == logical(rows)
        snapshots.append(snapshot)
    assert len({snapshot.summary["digest"] for snapshot in snapshots}) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "file", "bigquery", "braintrust"])
async def test_nested_native_schema_failure_is_classified_for_every_source(kind, tmp_path):
    rows = [
        {
            "id": "nested",
            "input": {
                "entries": [{"label": "one", "count": True}],
                "tags": [],
                "mode": "fast",
                "optional": None,
            },
            "expected": "x",
        }
    ]
    source = source_fixture(kind, rows, tmp_path)
    descriptor = dataset(
        name="nested-invalid",
        schema=case_schema(
            input=NativePayload,
            expected=str,
        ),
    )(lambda: source)
    with pytest.raises(DatasetError, match="SchemaError"):
        await collect_dataset(descriptor)


def logical(rows):
    return [
        {key: value for key, value in row.items() if key not in ("id", "label", "provenance")}
        for row in rows
    ]
