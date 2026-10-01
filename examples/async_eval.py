"""Async source, task, and scorer with typed nested inputs and task metadata."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

import mic
from mic import ScoreContext


@dataclass
class Item:
    text: str
    delay: float


@mic.dataset(name="async.items", schema=mic.case_schema(input=Item, expected=str))
async def items() -> AsyncIterator[object]:
    for index, delay in enumerate((0.03, 0.02, 0.01)):
        yield mic.RawCase(
            id=f"async-{index}", input=Item(f"item {index}", delay), expected=f"ITEM {index}"
        )


@mic.scorer(name="exact")
async def exact(context: ScoreContext[Item, str]) -> float:
    await asyncio.sleep(0)
    return float(context.output == context.require_expected())


@mic.eval(name="async.uppercase", dataset=items, output=str, scorers=[exact], concurrency=3)
async def uppercase(item: Item) -> str:
    await asyncio.sleep(item.delay)
    return item.text.upper()
