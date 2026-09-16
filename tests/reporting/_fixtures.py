from mic.models import JsonObject


def sample() -> tuple[JsonObject, list[JsonObject]]:
    manifest: JsonObject = {
        "schema_version": "mic-run-v1",
        "name": "<script>bad()</script>",
        "run_id": "run-1",
        "status": "completed",
        "dataset": {"name": "data", "rows": 2, "digest": "sha256:abc"},
        "counts": {"planned": 2, "completed": 2, "failed": 0, "cancelled": 0},
        "options": {"trials": 1, "concurrency": 2},
        "scores": {"exact": {"mean": 1.0, "count": 1, "null_count": 1, "unavailable_count": 0}},
        "gates": [],
        "provenance": {"definition": "example:eval"},
    }
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
    return manifest, cases
