"""Serialized, isolated event delivery with bounded failure receipts."""

import asyncio
import copy
from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace

from ..models import JsonObject
from ..results import EvaluationOutcome, Failure, RunInfo, SinkReceipt, failure, to_json
from ..sinks.base import ResultSink, RunEvent, SinkSession, TrialFinished
from .validation import dumps, json_object, nonempty


@dataclass(frozen=True)
class Scope:
    """Evaluations that declared a sink, and the sources those evaluations read."""

    tasks: frozenset[str]
    sources: frozenset[str]

    def receives(self, event: RunEvent) -> bool:
        if isinstance(event, TrialFinished):
            return event.task in self.tasks
        return event.source_id in self.sources

    def info(self, run: RunInfo) -> RunInfo:
        return replace(run, tasks={k: v for k, v in run.tasks.items() if k in self.tasks})


@dataclass(frozen=True)
class Selected:
    """A sink to open; ``scope=None`` receives every event in the run."""

    sink: ResultSink
    scope: Scope | None = None


@dataclass
class _OpenSink:
    name: str
    scope: Scope | None = None
    manager: AbstractAsyncContextManager[SinkSession] | None = None
    session: SinkSession | None = None
    events: int = 0
    receipt: SinkReceipt | None = None


def _outcome_copy(outcome: EvaluationOutcome) -> EvaluationOutcome:
    return replace(
        outcome, sources={key: copy.deepcopy(value) for key, value in outcome.sources.items()}
    )


class Delivery:
    def __init__(self) -> None:
        self._sinks: list[_OpenSink] = []
        self._lock = asyncio.Lock()

    @property
    def failed(self) -> bool:
        return any(s.receipt is not None and s.receipt.status != "completed" for s in self._sinks)

    async def open(self, sinks: Sequence[Selected], run: RunInfo) -> None:
        for selected in sinks:
            sink, scope = selected.sink, selected.scope
            state = _OpenSink(sink.name, scope)
            self._sinks.append(state)
            try:
                state.manager = sink.open(copy.deepcopy(run if scope is None else scope.info(run)))
                state.session = await state.manager.__aenter__()
            except asyncio.CancelledError as exc:
                state.receipt = SinkReceipt(
                    state.name, "cancelled", error=failure("sink_open", exc)
                )
                raise
            except Exception as exc:
                state.receipt = SinkReceipt(state.name, "failed", error=failure("sink_open", exc))
                break

    async def write(self, event: RunEvent) -> None:
        async with self._lock:
            for state in self._sinks:
                if state.session is None or state.receipt is not None:
                    continue
                if state.scope is not None and not state.scope.receives(event):
                    continue
                try:
                    await state.session.write(copy.deepcopy(event))
                    state.events += 1
                except asyncio.CancelledError as exc:
                    state.receipt = SinkReceipt(
                        state.name, "cancelled", state.events, error=failure("sink_write", exc)
                    )
                    raise
                except Exception as exc:
                    state.receipt = SinkReceipt(
                        state.name, "failed", state.events, error=failure("sink_write", exc)
                    )

    async def finish(
        self, outcome: EvaluationOutcome, scoped: Mapping[Scope, EvaluationOutcome]
    ) -> tuple[SinkReceipt, ...]:
        for state in self._sinks:
            if state.session is None:
                continue
            try:
                if state.receipt is None:
                    selected = outcome if state.scope is None else scoped[state.scope]
                    receipt = await state.session.finish(_outcome_copy(selected))
                    details: JsonObject = json_object(receipt.details)
                    error = receipt.error
                    if error is not None:
                        fields = json_object(to_json(error))
                        error = Failure(
                            nonempty("error phase", fields["phase"]),
                            nonempty("error type", fields["type"]),
                            nonempty("error message", fields["message"]),
                        )
                    receipt = replace(
                        receipt, name=state.name, events=state.events, details=details, error=error
                    )
                    if (
                        receipt.status not in ("completed", "failed", "cancelled")
                        or len(dumps(json_object(to_json(receipt))).encode("utf-8")) > 65_536
                    ):
                        raise ValueError("Invalid sink receipt")
                    state.receipt = receipt
            except asyncio.CancelledError as exc:
                state.receipt = SinkReceipt(
                    state.name, "cancelled", state.events, error=failure("sink_finish", exc)
                )
            except Exception as exc:
                state.receipt = SinkReceipt(
                    state.name, "failed", state.events, error=failure("sink_finish", exc)
                )
            finally:
                try:
                    assert state.manager is not None
                    await state.manager.__aexit__(None, None, None)
                except asyncio.CancelledError as exc:
                    state.receipt = SinkReceipt(
                        state.name, "cancelled", state.events, error=failure("sink_close", exc)
                    )
                except Exception as exc:
                    state.receipt = SinkReceipt(
                        state.name, "failed", state.events, error=failure("sink_close", exc)
                    )
        return tuple(s.receipt for s in self._sinks if s.receipt is not None)
