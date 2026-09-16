import pytest

import mic
from mic.validation import normalize_scores, numeric_stats


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([], {"count": 0, "mean": None, "min": None, "max": None, "p50": None, "p95": None}),
        ([0.5], {"count": 1, "mean": 0.5, "min": 0.5, "max": 0.5, "p50": 0.5, "p95": 0.5}),
        ([0, 1], {"count": 2, "mean": 0.5, "min": 0, "max": 1, "p50": 0, "p95": 1}),
        (
            [0, 0.25, 0.5, 0.75, 1],
            {"count": 5, "mean": 0.5, "min": 0, "max": 1, "p50": 0.5, "p95": 1},
        ),
        (
            list(range(21)),
            {"count": 21, "mean": 10.0, "min": 0, "max": 20, "p50": 10, "p95": 19},
        ),
        (
            [1, -1, 1, -1],
            {"count": 4, "mean": 0.0, "min": -1, "max": 1, "p50": -1, "p95": 1},
        ),
    ],
)
def test_numeric_statistics_use_nearest_rank(
    values: list[float], expected: dict[str, float | int | None]
) -> None:
    assert numeric_stats(values) == expected


@pytest.mark.parametrize(
    ("raw", "metrics", "expected"),
    [
        (
            mic.Score("exact", 0),
            ("exact",),
            [{"name": "exact", "value": 0, "metadata": {}}],
        ),
        (
            mic.Score("applicable", None),
            ("applicable",),
            [{"name": "applicable", "value": None, "metadata": {}}],
        ),
        (
            [mic.Score("a", 0.25), mic.Score("b", 1, {"reason": "match"})],
            ("a", "b"),
            [
                {"name": "a", "value": 0.25, "metadata": {}},
                {"name": "b", "value": 1, "metadata": {"reason": "match"}},
            ],
        ),
    ],
)
def test_valid_scores_normalize(
    raw: object, metrics: tuple[str, ...], expected: list[dict[str, object]]
) -> None:
    assert normalize_scores(raw, metrics) == expected


@pytest.mark.parametrize(
    "raw",
    [
        [],
        mic.Score("", 1),
        mic.Score(" ", 1),
        mic.Score("bad", True),
        mic.Score("bad", float("nan")),
        mic.Score("bad", float("inf")),
        mic.Score("bad", 1, []),
    ],
)
def test_invalid_scores_are_rejected(raw: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        normalize_scores(raw, ("bad",))


def test_duplicate_or_undeclared_metrics_are_rejected() -> None:
    with pytest.raises(ValueError, match="Duplicate"):
        normalize_scores([mic.Score("a", 1), mic.Score("a", 0)], ("a",))
    with pytest.raises(ValueError, match="declared"):
        normalize_scores(mic.Score("unexpected", 1), ("expected",))
