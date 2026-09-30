"""Explicit read-only smoke tests. No fixture discovery or implicit credential use.

Opt in separately:
  MIC_LIVE_BIGQUERY=1 MIC_BIGQUERY_PROJECT=... MIC_BIGQUERY_LOCATION=US
  MIC_BIGQUERY_SQL='SELECT ... ORDER BY id' MIC_BIGQUERY_MAX_BYTES=1000000

  MIC_LIVE_BRAINTRUST=1 MIC_BRAINTRUST_DATASET_ID=... MIC_BRAINTRUST_XACT_ID=...
  BRAINTRUST_API_KEY=... [BRAINTRUST_API_URL=...]

Both fixture datasets must contain at least two rows so pagination is exercised.
Cloud setup/mutation belongs to the fixture owner, never to source adapters.
"""

import json
import os

import pytest

from mic.models import ReadLimits
from mic.providers.bigquery import BigQueryHandle, BigQueryLoader
from mic.providers.braintrust import BraintrustHandle, BraintrustLoader


def required(name):
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"live integration was enabled but required fixture config {name} is absent")
    return value


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("MIC_LIVE_BIGQUERY") != "1", reason="live BigQuery opt-in absent"
)
async def test_live_bigquery_bounded_ordered_fixture(record_property):
    source = BigQueryHandle(
        billing_project=required("MIC_BIGQUERY_PROJECT"),
        location=required("MIC_BIGQUERY_LOCATION"),
        sql=required("MIC_BIGQUERY_SQL"),
        maximum_bytes_billed=int(required("MIC_BIGQUERY_MAX_BYTES")),
        page_size=1,
    )
    async with BigQueryLoader().open(source, limits=ReadLimits(max_rows=100)) as read:
        rows = [row async for row in read.rows()]
        assert len(rows) >= 2, "fixture must have >=2 rows to exercise pages"
        assert read.provenance["job_id"]
        record_property("provider_provenance", json.dumps(read.provenance))


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("MIC_LIVE_BRAINTRUST") != "1", reason="live Braintrust opt-in absent"
)
async def test_live_braintrust_pinned_multi_page_repeat(record_property):
    required("BRAINTRUST_API_KEY")
    source = BraintrustHandle(
        dataset_id=required("MIC_BRAINTRUST_DATASET_ID"),
        xact_id=required("MIC_BRAINTRUST_XACT_ID"),
        page_size=1,
    )
    snapshots = []
    for _ in range(2):
        async with BraintrustLoader().open(source, limits=ReadLimits(max_rows=100)) as read:
            rows = [row async for row in read.rows()]
            assert len(rows) >= 2, "fixture must have >=2 records to exercise pagination"
            assert read.provenance["pages"] >= 3
            snapshots.append(rows)
            record_property("provider_provenance", json.dumps(read.provenance))
    assert snapshots[0] == snapshots[1]
