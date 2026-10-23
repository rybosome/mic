"""Internal progress notifications; an observer can never change a run's outcome.

Observers receive small scalar views of run progress, not sink events: no case
payloads, outputs, or exception text. Calls are synchronous, so notifying adds no
await points and cannot change scheduling or cancellation.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from ..models import ReadLimits
from ..results import Failure, SourceSummary
from .aggregation import TrialObservation


@dataclass(frozen=True)
class TaskPlan:
    name: str
    scorers: tuple[str, ...]
    trials: int
    timeout: float | None


@dataclass(frozen=True)
class SourcePlan:
    source_id: str
    name: str
    # Evaluations that run every accepted case of this source, in execution order.
    tasks: tuple[str, ...]


@dataclass(frozen=True)
class RunPlan:
    run_id: str
    tasks: tuple[TaskPlan, ...]
    sources: tuple[SourcePlan, ...]
    concurrency: int
    limits: ReadLimits


@dataclass(frozen=True)
class TrialError:
    phase: str
    type: str
    scorer: str | None = None


class RunObserver(Protocol):
    def planned(self, run: RunPlan) -> None: ...

    def source_started(self, source_id: str) -> None: ...

    def case_accepted(self, source_id: str, row_index: int, label: str | None) -> None: ...

    def record_rejected(self, source_id: str, row_index: int, error: Failure) -> None: ...

    def trial_started(self, task: str, source_id: str, row_index: int, trial: int) -> None: ...

    # Queued trials drained by cancellation finish as "cancelled" without starting.
    def trial_finished(
        self,
        task: str,
        source_id: str,
        row_index: int,
        trial: int,
        observation: TrialObservation,
        errors: tuple[TrialError, ...],
    ) -> None: ...

    def source_finished(self, source_id: str, summary: SourceSummary) -> None: ...

    def cancelling(self) -> None: ...

    def finishing(self, sinks: tuple[str, ...]) -> None: ...


class Progress:
    """Forward notifications to an optional observer, dropping it on its first error."""

    def __init__(self, observer: RunObserver | None) -> None:
        self._observer = observer

    def __call__(self, notify: Callable[[RunObserver], object]) -> None:
        if self._observer is None:
            return
        try:
            notify(self._observer)
        except Exception:
            # Progress is advisory. A broken display must not fail, slow, or alter the run,
            # and a partially updated observer cannot be trusted with later events.
            self._observer = None
