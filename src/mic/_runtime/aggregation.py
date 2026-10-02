"""Online aggregation: storage scales with definitions, never trial count."""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from ..summaries import EvaluationSummary, Statistics, TaskSummary, TrialSummary

type TrialStatus = Literal["completed", "task_failed", "scoring_failed", "cancelled"]


@dataclass(slots=True)
class Moments:
    count: int = 0
    mean: float = 0.0
    minimum: float | None = None
    maximum: float | None = None

    def add(self, value: float) -> None:
        if isinstance(value, bool) or not math.isfinite(value):
            raise ValueError("Statistics require finite numeric observations")
        self.count += 1
        # Opposite signs can overflow the delta; same signs cannot. Avoid a
        # growing sum, which can overflow even when the mean is representable.
        if (self.mean < 0) != (value < 0):
            self.mean = self.mean * ((self.count - 1) / self.count) + value / self.count
        else:
            self.mean += (value - self.mean) / self.count
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)

    def snapshot(self) -> Statistics:
        return Statistics(self.count, self.mean if self.count else None, self.minimum, self.maximum)


@dataclass(frozen=True)
class TrialObservation:
    status: TrialStatus
    scores: Mapping[str, float | None]
    task_ms: float | None
    scoring_ms: float | None
    total_ms: float | None


@dataclass(slots=True)
class Trials:
    planned: int = 0
    completed: int = 0
    task_failed: int = 0
    scoring_failed: int = 0
    scoring_skipped: int = 0
    cancelled: int = 0
    task_ms: Moments = field(default_factory=Moments)
    scoring_ms: Moments = field(default_factory=Moments)
    total_ms: Moments = field(default_factory=Moments)

    def observe(self, trial: TrialObservation) -> None:
        setattr(self, trial.status, getattr(self, trial.status) + 1)
        self.scoring_skipped += any(value is None for value in trial.scores.values())
        if trial.task_ms is not None:
            self.task_ms.add(trial.task_ms)
        if trial.scoring_ms is not None:
            self.scoring_ms.add(trial.scoring_ms)
        if trial.total_ms is not None:
            self.total_ms.add(trial.total_ms)

    def snapshot(self) -> TrialSummary:
        return TrialSummary(
            self.planned,
            self.completed,
            self.task_failed,
            self.scoring_failed,
            self.scoring_skipped,
            self.cancelled,
            self.task_ms.snapshot(),
            self.scoring_ms.snapshot(),
            self.total_ms.snapshot(),
        )


class Aggregator:
    def __init__(self, tasks: Mapping[str, Sequence[str]]) -> None:
        self._global = Trials()
        self._trials = {name: Trials() for name in tasks}
        self._scores = {
            name: {metric: Moments() for metric in metrics} for name, metrics in tasks.items()
        }

    def admit(self, task: str) -> None:
        self._trials[task].planned += 1
        self._global.planned += 1

    def observe(self, task: str, trial: TrialObservation) -> None:
        scores = self._scores[task]
        unknown = trial.scores.keys() - scores.keys()
        if unknown:
            raise ValueError(f"Undeclared scores: {sorted(unknown)}")
        for metric, value in trial.scores.items():
            if value is not None:
                scores[metric].add(value)
        self._trials[task].observe(trial)
        self._global.observe(trial)

    def snapshot(self) -> EvaluationSummary:
        return EvaluationSummary(
            {
                name: TaskSummary(
                    {metric: moment.snapshot() for metric, moment in self._scores[name].items()},
                    trials.snapshot(),
                )
                for name, trials in self._trials.items()
            },
            self._global.snapshot(),
        )
