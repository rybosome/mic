"""Bounded, immutable summaries of evaluations and explicit requirements."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class Statistics:
    """Numeric observations only; empty observations have no mean or extrema."""

    count: int
    mean: float | None
    min: float | None
    max: float | None


@dataclass(frozen=True)
class TrialSummary:
    """Admitted trials, exclusive terminal outcomes, and observed phase timings.

    ``scoring_skipped`` overlaps terminal outcomes: it counts trials with at
    least one explicitly absent score, not scorers which never ran.
    """

    planned: int
    completed: int
    task_failed: int
    scoring_failed: int
    scoring_skipped: int
    cancelled: int
    task_ms: Statistics
    scoring_ms: Statistics
    total_ms: Statistics


@dataclass(frozen=True)
class TaskSummary:
    scores: Mapping[str, Statistics]
    trials: TrialSummary

    def __post_init__(self) -> None:
        object.__setattr__(self, "scores", MappingProxyType(dict(self.scores)))


@dataclass(frozen=True)
class EvaluationSummary:
    tasks: Mapping[str, TaskSummary]
    trials: TrialSummary

    def __post_init__(self) -> None:
        object.__setattr__(self, "tasks", MappingProxyType(dict(self.tasks)))


@dataclass(frozen=True)
class RequirementResult:
    expression: str
    actual: int | float | None
    passed: bool
    reason: str | None = None
