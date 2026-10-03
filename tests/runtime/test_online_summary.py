import math
import statistics
from dataclasses import FrozenInstanceError

import pytest

import mic
from mic._runtime.aggregation import Aggregator, Moments, TrialObservation


@pytest.mark.parametrize(
    "values", [[], [1.0], [0, 1, 0, 1], [-5, 0, 8], [1e308, 1e308], [-1e308, 1e308]]
)
def test_online_statistics(values: list[float]) -> None:
    accumulator = Moments()
    for value in values:
        accumulator.add(value)
    result = accumulator.snapshot()
    assert result.count == len(values)
    if values:
        assert result.mean == pytest.approx(statistics.mean(values))
        assert result.min == min(values)
        assert result.max == max(values)
    else:
        assert result == mic.Statistics(0, None, None, None)


@pytest.mark.parametrize("value", [True, math.inf, -math.inf, math.nan])
def test_nonfinite_observations_rejected(value: float) -> None:
    with pytest.raises(ValueError, match="finite numeric"):
        Moments().add(value)


def test_trial_outcomes_missing_timings_and_weighted_global_statistics() -> None:
    aggregator = Aggregator({"a": ["accuracy", "applicable"], "b": ["accuracy"]})
    observations = [
        ("a", TrialObservation("completed", {"accuracy": 1, "applicable": None}, 1, 2, 3)),
        ("a", TrialObservation("scoring_failed", {"accuracy": 0, "applicable": None}, 3, 4, 7)),
        ("a", TrialObservation("task_failed", {}, 5, None, 5)),
        ("a", TrialObservation("cancelled", {}, None, None, 0)),
        ("b", TrialObservation("completed", {"accuracy": 1}, 100, 20, 120)),
    ]
    for task, trial in observations:
        aggregator.admit(task)
        aggregator.observe(task, trial)
    result = aggregator.snapshot()
    assert result.trials.planned == 5
    assert result.trials.completed == 2
    assert result.trials.task_failed == result.trials.scoring_failed == result.trials.cancelled == 1
    assert result.trials.scoring_skipped == 2
    assert result.trials.task_ms == mic.Statistics(4, 27.25, 1, 100)
    assert result.trials.scoring_ms.count == 3
    assert result.tasks["a"].scores["accuracy"] == mic.Statistics(2, 0.5, 0, 1)
    assert result.tasks["a"].scores["applicable"].count == 0
    assert result.tasks["b"].trials.task_ms.mean == 100


def test_snapshots_are_isolated_immutable_and_bounded() -> None:
    aggregator = Aggregator({"a": ["accuracy"]})
    before = aggregator.snapshot()
    trial = TrialObservation("completed", {"accuracy": 1}, 1, 1, 2)
    for _ in range(100_000):
        aggregator.admit("a")
        aggregator.observe("a", trial)
    after = aggregator.snapshot()
    assert before.trials.planned == 0
    assert after.trials.planned == 100_000
    assert after.tasks["a"].scores["accuracy"].count == 100_000
    with pytest.raises(TypeError):
        after.tasks["new"] = after.tasks["a"]
    with pytest.raises(TypeError):
        after.tasks["a"].scores["new"] = mic.Statistics(0, None, None, None)
    with pytest.raises(FrozenInstanceError):
        after.trials.completed = 0
    tasks = dict(after.tasks)
    detached = mic.EvaluationSummary(tasks, after.trials)
    tasks.clear()
    assert "a" in detached.tasks


def test_undeclared_metrics_do_not_expand_summary() -> None:
    aggregator = Aggregator({"a": []})
    with pytest.raises(ValueError, match="Undeclared"):
        aggregator.observe("a", TrialObservation("completed", {"unexpected": 1}, 1, 1, 2))
    assert not aggregator.snapshot().tasks["a"].scores
