from mic.models import JsonObject


def sample() -> tuple[JsonObject, list[JsonObject]]:
    from mic._runtime.aggregation import Aggregator, TrialObservation
    from mic.results import RunInfo, RunResult, SourceSummary

    agg = Aggregator({"<script>bad()</script>": ["exact"]})
    for score in (None, 1.0):
        agg.admit("<script>bad()</script>")
        agg.observe(
            "<script>bad()</script>", TrialObservation("completed", {"exact": score}, 1.0, 1.0, 2.0)
        )
    manifest = RunResult(
        "run-1",
        "completed",
        0,
        agg.snapshot(),
        {"source": SourceSummary("data", 2, 2, 0, True, None)},
        (),
        info=RunInfo("run-1", "2026-10-02T00:00:00+00:00", {}),
    ).to_json()
    cases: list[JsonObject] = [
        {
            "case_id": "missing",
            "row_index": 0,
            "trial": 1,
            "status": "completed",
            "input": None,
            "output": None,
            "scores": [{"name": "exact", "value": None, "metadata": {"why": "none"}}],
            "errors": [],
            "latency": {"task_ms": 1.0, "total_ms": 2.0},
            "provenance": {"line": 1},
        },
        {
            "case_id": "null",
            "row_index": 1,
            "trial": 1,
            "status": "completed",
            "input": "hello",
            "expected": None,
            "output": '</script><script>alert("x")</script>\u2028&',
            "scores": [{"name": "exact", "value": 1.0}],
            "metadata": ["arbitrary", 42],
            "errors": [],
            "latency": {"task_ms": 1.0},
            "provenance": {"line": 2},
        },
    ]
    for case in cases:
        case["task"] = "<script>bad()</script>"
        case["source_id"] = "source"
    return manifest, cases
