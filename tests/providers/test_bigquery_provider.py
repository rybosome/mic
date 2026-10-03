import asyncio
import threading
from dataclasses import dataclass, field
from decimal import Decimal
from functools import partial

import pytest

from mic.errors import ConfigurationError, DatasetError
from mic.models import ReadLimits
from mic.providers.bigquery import BigQueryHandle, BigQueryParameter
from tests.providers.helpers import collect, open_bigquery


@dataclass
class Job:
    rows: list[dict[str, object]] = field(default_factory=lambda: [{"input": "x", "expected": "y"}])
    statement_type: str | None = "SELECT"
    total_bytes_processed: int | None = 25
    total_bytes_billed: int | None = 25
    job_id: str = "fixture-job"
    cache_hit: bool | None = False
    cancelled: bool = False
    result_options: dict = field(default_factory=dict)

    def result(self, **kwargs):
        self.result_options = kwargs
        return iter(self.rows)

    def cancel(self):
        self.cancelled = True
        return True


class Client:
    def __init__(self, dry=None, job=None):
        self.dry = dry or Job()
        self.job = job or Job()
        self.calls = []
        self.closed = False

    def query(self, query, **kwargs):
        self.calls.append((query, kwargs))
        return self.dry if kwargs["job_config"]["dry_run"] else self.job

    def close(self):
        self.closed = True


def config(handle, dry):
    return {
        "dry_run": dry,
        "maximum_bytes_billed": handle.maximum_bytes_billed,
        "parameters": handle.parameters,
    }


def handle(**kwargs):
    return BigQueryHandle(
        billing_project="test-project",
        location="EU",
        sql="WITH cases AS (SELECT @value AS input) SELECT * FROM cases ORDER BY input",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_dry_run_before_execution_and_parameter_provenance():
    client = Client()
    loader = partial(open_bigquery, client=client, config_factory=config)
    source = handle(parameters=(BigQueryParameter("value", "STRING", "x"),))
    async with loader(source, limits=ReadLimits()) as read:
        assert [row async for row in read.rows()] == [{"input": "x", "expected": "y"}]
        assert read.provenance["job_id"] == "fixture-job"
        assert read.provenance["estimated_bytes_processed"] == 25
        assert read.provenance["parameters"] == [
            {"name": "value", "sql_type": "STRING", "value": "x"}
        ]
        digest = read.provenance["query_sha256"]
    assert not client.closed
    assert [call[1]["job_config"]["dry_run"] for call in client.calls] == [True, False]
    assert all(call[1]["location"] == "EU" for call in client.calls)
    assert client.job.result_options == {"page_size": 1000, "timeout": 60.0}
    _, again = await collect(loader, source)
    assert again["query_sha256"] == digest
    _, changed = await collect(
        loader, handle(parameters=(BigQueryParameter("value", "STRING", "other"),))
    )
    assert changed["query_sha256"] != digest


@pytest.mark.asyncio
@pytest.mark.parametrize("statement_type", ["SCRIPT", "INSERT", "CREATE_TABLE", None])
async def test_non_select_metadata_prevents_execution(statement_type):
    client = Client(dry=Job(statement_type=statement_type))
    loader = partial(open_bigquery, client_factory=lambda _: client, config_factory=config)
    with pytest.raises(DatasetError, match="read-only SELECT"):
        async with loader(handle(), limits=ReadLimits()) as read:
            _ = [row async for row in read.rows()]
    assert len(client.calls) == 1
    assert client.closed


@pytest.mark.asyncio
async def test_dry_run_cost_cap_and_cleanup():
    client = Client(dry=Job(total_bytes_processed=101))
    loader = partial(open_bigquery, client_factory=lambda _: client, config_factory=config)
    with pytest.raises(DatasetError, match="exceeding maximum_bytes_billed=100"):
        await collect(loader, handle(maximum_bytes_billed=100))
    assert len(client.calls) == 1
    assert client.closed


@pytest.mark.asyncio
async def test_raw_column_types_preserved_for_mapper_and_budget_failure_cancels():
    native = Decimal("1.25")
    client = Client(job=Job(rows=[{"cost": native}]))
    loader = partial(open_bigquery, client=client, config_factory=config)
    async with loader(handle(), limits=ReadLimits()) as read:
        rows = [row async for row in read.rows()]
    assert rows[0]["cost"] is native
    client = Client(job=Job(rows=[{"input": "x"}, {"input": "y"}]))
    loader = partial(open_bigquery, client_factory=lambda _: client, config_factory=config)
    with pytest.raises(DatasetError, match="max_rows=1"):
        async with loader(handle(), limits=ReadLimits(max_rows=1)) as read:
            _ = [row async for row in read.rows()]
    assert client.job.cancelled
    assert client.closed
    assert client.job.result_options["page_size"] == 2


@pytest.mark.asyncio
async def test_bigquery_accepts_records_larger_than_one_mib():
    value = "x" * (1024 * 1024 + 1)
    client = Client(job=Job(rows=[{"input": value}]))
    loader = partial(open_bigquery, client=client, config_factory=config)
    async with loader(handle(), limits=ReadLimits()) as read:
        rows = [row async for row in read.rows()]
    assert rows == [{"input": value}]


@pytest.mark.asyncio
async def test_invalid_configuration_does_not_create_client():
    def forbidden(_):
        pytest.fail("client created before validation")

    loader = partial(open_bigquery, client_factory=forbidden)
    for source in (
        handle(maximum_bytes_billed=0),
        handle(timeout=float("inf")),
        handle(parameters=(BigQueryParameter("x", "STRUCT", {}),)),
        handle(parameters=(BigQueryParameter("x", "ARRAY<STRING>", "wrong"),)),
    ):
        with pytest.raises(ConfigurationError):
            await collect(loader, source)


@pytest.mark.asyncio
async def test_cancel_during_query_submission_cancels_returned_job_and_closes_client():
    started = threading.Event()
    released = threading.Event()

    class SlowClient(Client):
        def query(self, query, **kwargs):
            result = super().query(query, **kwargs)
            if not kwargs["job_config"]["dry_run"]:
                started.set()
                assert released.wait(2)
            return result

    client = SlowClient()
    loader = partial(open_bigquery, client_factory=lambda _: client, config_factory=config)

    async def consume():
        async with loader(handle(), limits=ReadLimits()) as read:
            return [row async for row in read.rows()]

    task = asyncio.create_task(consume())
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    released.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.closed
    assert client.job.cancelled


def test_installed_sdk_config_boundary_without_credentials():
    sdk = pytest.importorskip("google.cloud.bigquery", reason="optional BigQuery SDK not installed")
    from mic.providers.bigquery import _config

    source = handle(
        parameters=(
            BigQueryParameter("value", "STRING", "x"),
            BigQueryParameter("ids", "ARRAY<INT64>", [1, 2]),
        )
    )
    for dry in (True, False):
        config = _config(source, dry)
        assert isinstance(config, sdk.QueryJobConfig)
        assert config.dry_run is dry
        assert config.maximum_bytes_billed == 100_000_000
        assert config.use_legacy_sql is False
        assert config.use_query_cache is False
        assert config.query_parameters[0].to_api_repr()["parameterValue"] == {"value": "x"}
        assert config.query_parameters[1].to_api_repr()["parameterType"] == {
            "type": "ARRAY",
            "arrayType": {"type": "INT64"},
        }


@pytest.mark.asyncio
async def test_installed_sdk_row_mapping_boundary():
    sdk = pytest.importorskip("google.cloud.bigquery", reason="optional BigQuery SDK not installed")
    client = Client(job=Job(rows=[sdk.Row(("x", "y"), {"input": 0, "expected": 1})]))
    async with partial(open_bigquery, client=client, config_factory=config)(
        handle(), limits=ReadLimits()
    ) as read:
        assert [row async for row in read.rows()] == [{"input": "x", "expected": "y"}]
