"""Public event and sink contract; writes are serial and apply backpressure."""

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol

from ..models import JsonObject
from ..results import EvaluationOutcome, Failure, RunInfo, SinkReceipt, SourceSummary


@dataclass(frozen=True)
class CaseAccepted:
    source_id: str
    row_index: int
    case_id: str
    case: JsonObject


@dataclass(frozen=True)
class RecordRejected:
    source_id: str
    row_index: int
    error: Failure


@dataclass(frozen=True)
class TrialFinished:
    task: str
    source_id: str
    row_index: int
    trial: int
    case_id: str
    result: JsonObject


@dataclass(frozen=True)
class SourceFinished:
    source_id: str
    summary: SourceSummary


type RunEvent = CaseAccepted | RecordRejected | TrialFinished | SourceFinished


class SinkSession(Protocol):
    async def write(self, event: RunEvent) -> None: ...

    async def finish(self, outcome: EvaluationOutcome) -> SinkReceipt: ...


class ResultSink(Protocol):
    @property
    def name(self) -> str: ...

    def open(self, run: RunInfo) -> AbstractAsyncContextManager[SinkSession]: ...
