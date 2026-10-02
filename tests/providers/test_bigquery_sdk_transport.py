"""Exercise Client, QueryJob, and RowIterator with only the HTTP boundary replaced."""

import asyncio
import copy
import threading
from decimal import Decimal
from functools import partial

import pytest

from mic.errors import DatasetError
from mic.models import ReadLimits
from mic.providers.bigquery import BigQueryHandle, BigQueryParameter
from tests.providers.helpers import open_bigquery


class QueryAPI:
    def __init__(self):
        self.calls = []
        self.statement_type = "SELECT"
        self.query_ref = None
        self.page_failure = None
        self.dry_failure = None
        self.page_started = threading.Event()
        self.page_released = threading.Event()
        self.page_finished = threading.Event()
        self.block_page = False
        self.cancelled_before_read_finished = False

    def __call__(self, **request):
        self.calls.append(copy.deepcopy(request))
        method, path = request["method"], request["path"]
        if method == "POST" and path.endswith("/jobs"):
            data = request["data"]
            dry = data["configuration"].get("dryRun", False)
            if dry and self.dry_failure:
                raise self.dry_failure
            if not dry:
                self.query_ref = data["jobReference"]
            return {
                "kind": "bigquery#job",
                "jobReference": data["jobReference"],
                "configuration": data["configuration"],
                "status": {"state": "DONE"},
                "statistics": {
                    "query": {
                        "statementType": self.statement_type,
                        "totalBytesProcessed": "25",
                        "totalBytesBilled": "25",
                        "cacheHit": False,
                    }
                },
            }
        if method == "POST" and path.endswith("/cancel"):
            if self.block_page and not self.page_finished.is_set():
                self.cancelled_before_read_finished = True
            return {"job": {"jobReference": self.query_ref, "status": {"state": "DONE"}}}
        if method == "GET" and "/queries/" in path:
            token = request.get("query_params", {}).get("pageToken")
            index = {None: 0, "page-two": 1, "page-three": 2}[token]
            if index == 1:
                if self.block_page:
                    self.page_started.set()
                    assert self.page_released.wait(3), "test must release blocked HTTP page"
                    self.page_finished.set()
                if self.page_failure:
                    raise self.page_failure
            record_id = ("case-z", "case-a", "case-m")[index]
            value = {
                "kind": "bigquery#getQueryResultsResponse",
                "jobReference": self.query_ref,
                "jobComplete": True,
                "totalRows": "3",
                "schema": {
                    "fields": [
                        {"name": "id", "type": "STRING"},
                        {"name": "input", "type": "STRING"},
                        {"name": "expected", "type": "STRING"},
                        {"name": "cost", "type": "NUMERIC"},
                    ]
                },
                "rows": [{"f": [{"v": record_id}, {"v": "x"}, {"v": "y"}, {"v": "1.25"}]}],
            }
            if index < 2:
                value["pageToken"] = ("page-two", "page-three")[index]
            return value
        raise AssertionError(f"unexpected BigQuery request: {method} {path}")


@pytest.fixture
def actual_sdk(monkeypatch):
    sdk = pytest.importorskip("google.cloud.bigquery", reason="optional BigQuery SDK not installed")
    from google.auth.credentials import AnonymousCredentials

    monkeypatch.setattr("google.auth.default", lambda **_: pytest.fail("unexpected ADC discovery"))
    client = sdk.Client(
        project="fixture-project", location="EU", credentials=AnonymousCredentials()
    )
    api = QueryAPI()
    monkeypatch.setattr(client._connection, "api_request", api)
    closed = []
    real_close = client.close

    def close():
        closed.append(True)
        real_close()

    monkeypatch.setattr(client, "close", close)
    yield client, api, closed
    real_close()


def source():
    return BigQueryHandle(
        billing_project="fixture-project",
        location="EU",
        sql="SELECT id, input, expected, cost FROM cases WHERE team = @team ORDER BY ordinal",
        parameters=(BigQueryParameter("team", "STRING", "evals"),),
        maximum_bytes_billed=1000,
        page_size=1,
    )


@pytest.mark.asyncio
async def test_actual_sdk_dry_run_pagination_preserves_server_order_and_native_types(actual_sdk):
    client, api, closed = actual_sdk
    async with partial(open_bigquery, client_factory=lambda _: client)(
        source(), limits=ReadLimits()
    ) as read:
        rows = [row async for row in read.rows()]
        assert read.provenance["total_bytes_processed"] == 25
        assert read.provenance["total_bytes_billed"] == 25
    assert [row["id"] for row in rows] == ["case-z", "case-a", "case-m"]
    assert all(row["cost"] == Decimal("1.25") for row in rows)
    jobs = [call for call in api.calls if call["path"].endswith("/jobs")]
    assert [call["data"]["configuration"].get("dryRun", False) for call in jobs] == [True, False]
    for call in jobs:
        config = call["data"]["configuration"]["query"]
        assert config["maximumBytesBilled"] == "1000"
        assert config["queryParameters"][0]["parameterValue"] == {"value": "evals"}
        assert call["data"]["jobReference"]["location"] == "EU"
    pages = [call for call in api.calls if "/queries/" in call["path"]]
    assert [call["query_params"].get("pageToken") for call in pages] == [
        None,
        "page-two",
        "page-three",
    ]
    assert all(call["query_params"]["maxResults"] == 1 for call in pages)
    assert closed == [True]


@pytest.mark.asyncio
async def test_actual_sdk_rejects_dry_run_permission_error_before_execution(actual_sdk):
    from google.api_core.exceptions import Forbidden

    client, api, closed = actual_sdk
    api.dry_failure = Forbidden("fixture dataset permission denied")
    with pytest.raises(DatasetError, match="BigQuery dataset read failed"):
        async with partial(open_bigquery, client_factory=lambda _: client)(
            source(), limits=ReadLimits()
        ) as read:
            _ = [row async for row in read.rows()]
    assert len(api.calls) == 1
    assert closed == [True]


@pytest.mark.asyncio
async def test_actual_sdk_later_page_error_cancels_job_and_closes_owned_client(actual_sdk):
    from google.api_core.exceptions import Forbidden

    client, api, closed = actual_sdk
    api.page_failure = Forbidden("fixture page permission denied")
    with pytest.raises(DatasetError, match="BigQuery dataset read failed"):
        async with partial(open_bigquery, client_factory=lambda _: client)(
            source(), limits=ReadLimits()
        ) as read:
            _ = [row async for row in read.rows()]
    assert sum(call["path"].endswith("/cancel") for call in api.calls) == 1
    assert sum(call["path"].endswith("/jobs") for call in api.calls) == 2
    assert closed == [True]


@pytest.mark.asyncio
async def test_actual_sdk_repeated_cancel_joins_page_before_cancelling_job(actual_sdk):
    client, api, closed = actual_sdk
    api.block_page = True

    async def consume():
        async with partial(open_bigquery, client_factory=lambda _: client)(
            source(), limits=ReadLimits()
        ) as read:
            return [row async for row in read.rows()]

    task = asyncio.create_task(consume())
    try:
        assert await asyncio.to_thread(api.page_started.wait, 2)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
        assert not closed
    finally:
        api.page_released.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert api.page_finished.is_set()
    assert not api.cancelled_before_read_finished
    assert sum(call["path"].endswith("/cancel") for call in api.calls) == 1
    assert closed == [True]
