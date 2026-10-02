import pytest

import mic
from mic._runtime.validation import normalize_score


@pytest.mark.parametrize(
    ("raw", "name", "expected"),
    [
        (
            0,
            "exact",
            {"name": "exact", "value": 0, "metadata": {}},
        ),
        (
            None,
            "applicable",
            {"name": "applicable", "value": None, "metadata": {}},
        ),
        (
            mic.Score(1, {"reason": "match"}),
            "evidence",
            {"name": "evidence", "value": 1, "metadata": {"reason": "match"}},
        ),
    ],
)
def test_valid_scores_normalize(raw: object, name: str, expected: dict[str, object]) -> None:
    assert normalize_score(raw, name) == expected


@pytest.mark.parametrize(
    "raw",
    [
        [],
        True,
        float("nan"),
        float("inf"),
        mic.Score(1, []),
    ],
)
def test_invalid_scores_are_rejected(raw: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        normalize_score(raw, "bad")
