"""Provider test fixtures inject transport settings onto public source objects."""

from dataclasses import replace

from mic._runtime.sources import open_source
from mic.models import ReadLimits


def open_bigquery(source, *, limits=ReadLimits(), **settings):
    return open_source(replace(source, **settings), limits=limits)


def open_braintrust(source, *, limits=ReadLimits(), **settings):
    return open_source(replace(source, **settings), limits=limits)


async def collect(open_read, source):
    async with open_read(source) as read:
        rows = [row async for row in read.rows()]
        return rows, read.provenance
