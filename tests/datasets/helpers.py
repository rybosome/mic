"""Tests may collect a small stream; production execution never creates snapshots."""

from types import SimpleNamespace

import mic
from mic._runtime.datasets import DatasetReader, describe_dataset
from mic.results import to_json


async def collect_dataset(dataset, *, limits=None, limit=None):
    describe_dataset(dataset)
    reader = DatasetReader(dataset, "fixture-source", limits or mic.ReadLimits())
    iterator = reader.rows()
    cases = []
    rows = []
    try:
        async for item in iterator:
            if len(item) == 2:
                raise mic.DatasetError(item[1].message)
            _, case, normalized = item
            cases.append(case)
            rows.append(normalized)
            if limit is not None and len(rows) >= limit:
                break
    finally:
        await iterator.aclose()
    return SimpleNamespace(cases=cases, rows=rows, summary=to_json(reader.summary()))
