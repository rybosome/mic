"""Opt-in local event journal; final run publication occurs after sinks settle."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO, cast

from ..errors import ConfigurationError
from ..models import JsonObject
from ..results import EvaluationOutcome, RunInfo, SinkReceipt, to_json
from ..sinks.base import CaseAccepted, RecordRejected, RunEvent, SourceFinished, TrialFinished
from .files import atomic_write
from .validation import dumps

_EVENT_NAMES = {
    CaseAccepted: "case_accepted",
    RecordRejected: "record_rejected",
    TrialFinished: "trial_finished",
    SourceFinished: "source_finished",
}


def event_json(event: RunEvent) -> JsonObject:
    return {
        "schema_version": "mic-event-v1",
        "type": _EVENT_NAMES[type(event)],
        **cast(JsonObject, to_json(event)),
    }


@dataclass
class _Session:
    stream: TextIO
    events: int = 0

    async def write(self, event: RunEvent) -> None:
        self.stream.write(dumps(event_json(event)) + "\n")
        self.stream.flush()
        self.events += 1

    async def finish(self, outcome: EvaluationOutcome) -> SinkReceipt:
        self.stream.flush()
        return SinkReceipt("jsonl", "completed", self.events)


@dataclass(frozen=True)
class JsonlSink:
    path: Path
    name: str = "jsonl"
    acquired: bool = field(default=False, init=False)

    @asynccontextmanager
    async def open(self, run: RunInfo) -> AsyncGenerator[_Session]:
        if self.path.exists() and (not self.path.is_dir() or any(self.path.iterdir())):
            raise ConfigurationError(f"Output directory must be empty: {self.path}")
        self.path.mkdir(parents=True, exist_ok=True)
        with (self.path / "events.jsonl").open("x", encoding="utf-8") as stream:
            object.__setattr__(self, "acquired", True)
            atomic_write(
                self.path / "run.json",
                (
                    dumps(
                        {
                            "schema_version": "mic-run-v3",
                            "run_id": run.run_id,
                            "status": "running",
                        }
                    )
                    + "\n",
                ),
            )
            yield _Session(stream)
