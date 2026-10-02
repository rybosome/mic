"""Serialized, isolated event delivery with bounded failure receipts."""

import asyncio
import copy
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace

from ..models import JsonObject
from ..results import EvaluationOutcome, Failure, RunInfo, SinkReceipt, failure, to_json
from ..sinks.base import ResultSink, RunEvent, SinkSession
from .validation import dumps, json_object, nonempty


@dataclass
class _OpenSink:
    name: str
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

    async def open(self, sinks: Sequence[ResultSink], run: RunInfo) -> None:
        for sink in sinks:
            state = _OpenSink(sink.name)
            self._sinks.append(state)
            try:
                state.manager = sink.open(copy.deepcopy(run))
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

    async def finish(self, outcome: EvaluationOutcome) -> tuple[SinkReceipt, ...]:
        for state in self._sinks:
            if state.session is None:
                continue
            try:
                if state.receipt is None:
                    receipt = await state.session.finish(_outcome_copy(outcome))
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
