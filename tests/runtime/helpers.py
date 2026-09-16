"""Small public-API fixtures shared by runtime behavior tests."""

import json
from pathlib import Path

import mic


def evaluation(rows, *, task=None, scorers=None, schema=None, output=object, **options):
    @mic.dataset(name="fixture", schema=schema or mic.case_schema(input=object, expected=object))
    def data():
        return rows

    @mic.scorer(name="exact")
    def exact(ctx: mic.ScoreContext[object, object, object, mic.JsonObject]):
        return mic.Score("exact", float(ctx.output == ctx.require_expected()))

    callback = task or (lambda _ctx, value: value)
    return mic.eval(
        name="runtime",
        dataset=data,
        output=output,
        scorers=[exact] if scorers is None else scorers,
        **options,
    )(callback)


def row(value=1, **extra):
    return {"id": "a", "input": value, "expected": value, **extra}


def manifest(path: Path):
    return json.loads((path / "run.json").read_text())
