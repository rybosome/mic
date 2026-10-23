"""The human summary is readable, aligned, and never contradicts the exact evidence."""

import copy

from mic.reporters.console import format_summary

from ._fixtures import sample


def scored(**metrics: tuple[float | None, int]):
    manifest, _ = sample()
    manifest = copy.deepcopy(manifest)
    task = next(iter(manifest["summary"]["tasks"].values()))
    task["scores"] = {
        name: {"count": count, "mean": mean, "min": mean, "max": mean}
        for name, (mean, count) in metrics.items()
    }
    return manifest


def score_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("Score")]


def test_score_means_are_rounded_and_aligned() -> None:
    manifest = scored(
        exact=(0.976207729468599, 30),
        unneeded_recall=(0.6666666666666666, 12),
        degraded=(0.0, 30),
    )
    task = next(iter(manifest["summary"]["tasks"]))
    assert score_lines(format_summary(manifest)) == [
        f"Score        {task}.exact            mean 0.976  count 30",
        f"Score        {task}.unneeded_recall  mean 0.667  count 12",
        f"Score        {task}.degraded         mean 0.000  count 30",
    ]


def test_extreme_and_missing_means_stay_honest() -> None:
    manifest = scored(tiny=(0.0004, 3), huge=(12_345_678.0, 3), negative=(-0.25, 3), none=(None, 0))
    means = [
        line.split("mean")[1].split("count")[0].strip()
        for line in score_lines(format_summary(manifest))
    ]
    # Values that 3 decimals would flatten to 0.000, or that are very large, use exponents.
    assert means == ["4.000e-04", "1.235e+07", "-0.250", "n/a"]


def test_elapsed_time_is_shown_only_when_measured() -> None:
    manifest, _ = sample()
    assert "Elapsed" not in format_summary(manifest)
    lines = format_summary(manifest, elapsed=182.4).splitlines()
    assert lines[1].startswith("Status       ")
    assert lines[2] == "Elapsed      3:02"
    assert format_summary(manifest, elapsed=4.25).splitlines()[2] == "Elapsed      4.2s"


def test_requirement_actuals_are_exact() -> None:
    manifest = scored(exact=(0.8999999, 10))
    manifest["requirements"] = [
        {
            "expression": 'tasks["t"].scores["exact"].mean>=0.9',
            "path": ["tasks", "t", "scores", "exact", "mean"],
            "operator": ">=",
            "threshold": 0.9,
            "actual": 0.8999999,
            "passed": False,
        }
    ]
    text = format_summary(manifest)
    assert "mean 0.900" in text
    assert 'Requirement  FAIL tasks["t"].scores["exact"].mean>=0.9 (actual: 0.8999999)' in text
