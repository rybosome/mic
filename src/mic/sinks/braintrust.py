"""Incremental, opt-in Braintrust export; never re-executes evaluations."""

import copy
import importlib
import json
import math
import os
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from typing import Any, cast
from urllib.parse import urlparse

from mic._runtime.callbacks import CallbackPool
from mic.errors import ConfigurationError
from mic.models import JsonObject
from mic.results import EvaluationOutcome, RunInfo, SinkReceipt
from mic.sinks.base import RunEvent, TrialFinished


def _synchronous_state(sdk: Any) -> Any:
    """Own the SDK logger so export failures cannot be silently dropped.

    Braintrust 0.39.0's default background mode logs and drops failed requests.
    The supported advanced ``init(state=...)`` hook isolates our configuration;
    the logger's sync_flush attribute is a deliberately version-pinned boundary.
    Creating the state/logger does not authenticate or perform network requests.
    """
    factory = getattr(sdk, "BraintrustState", None)
    if not callable(factory):
        raise ConfigurationError("Braintrust export requires the pinned SDK BraintrustState API")
    state: Any = factory()
    logger: Any = state.global_bg_logger()
    if not hasattr(logger, "sync_flush") or not callable(
        getattr(logger, "enforce_queue_size_limit", None)
    ):
        raise ConfigurationError(
            "Braintrust SDK logger does not support reliable synchronous export"
        )
    logger.sync_flush = True
    # The sink flushes one bounded trial at a time. A small SDK queue must not drop spans
    # before flush gets a chance to surface a remote failure.
    logger.enforce_queue_size_limit(False)
    return state


def _events(manifest: JsonObject, cases: Sequence[JsonObject]) -> list[JsonObject]:
    """Validate the entire projection before the first remote write."""
    events: list[JsonObject] = []
    for case in cases:
        scores: JsonObject = {}
        score_rows = case.get("scores", [])
        if not isinstance(score_rows, list):
            raise ConfigurationError("Braintrust export: case scores must be an array")
        for score in score_rows:
            if not isinstance(score, dict) or not isinstance(score.get("name"), str):
                raise ConfigurationError("Braintrust export: invalid score record")
            name = str(score["name"])
            if name in scores:
                raise ConfigurationError(f"Braintrust export: duplicate score {name!r}")
            value = score.get("value")
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0 <= value <= 1
            ):
                raise ConfigurationError(
                    f"Braintrust only accepts scores in [0, 1]; {name!r} for "
                    f"case {case.get('case_id')!r} is {value!r}. Local scores are unchanged."
                )
            scores[name] = value
        # Namespaced evidence cannot collide with dataset metadata keys. The complete case keeps
        # absent expected/output distinct from explicit null even if a remote UI hides null.
        evidence: JsonObject = {
            "run_id": manifest.get("run_id"),
            "dataset": manifest.get("dataset"),
            "case": dict(case),
        }
        event: JsonObject = {"scores": scores, "metadata": {"mic": evidence}}
        for key in ("input", "expected", "output"):
            if key in case:
                event[key] = case[key]
        errors = case.get("errors")
        if errors:
            event["error"] = json.dumps(errors, ensure_ascii=False, allow_nan=False)
        latency = case.get("latency")
        if isinstance(latency, dict):
            metrics: JsonObject = {}
            for key, value in latency.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    metrics[f"mic_{key}"] = value
            event["metrics"] = metrics
        # The SDK may normalize its payload in place. Keep the authoritative local
        # manifest/cases isolated from that third-party mutation boundary.
        events.append(copy.deepcopy(event))
    return events


class BraintrustSink:
    """Opt-in streaming experiment sink; a failed run can leave partial remote evidence."""

    name = "braintrust"

    def __init__(
        self,
        *,
        project: str,
        experiment: str | None = None,
        api_key: str | None = None,
        app_url: str | None = None,
        org_name: str | None = None,
        sdk: Any = None,
    ) -> None:
        # Any exists only at the optional third-party SDK boundary.
        self.project = project
        self.experiment = experiment
        self._api_key = api_key
        self.app_url = app_url
        self.org_name = org_name
        self._sdk: Any = sdk
        self._injected = sdk is not None

    async def prepare(self) -> None:
        if not self.project.strip():
            raise ConfigurationError("Braintrust sink requires a nonempty project")
        if self.experiment is not None and not self.experiment.strip():
            raise ConfigurationError("Braintrust experiment must be nonempty when supplied")
        if self.app_url is not None:
            parsed = urlparse(self.app_url)
            if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
                raise ConfigurationError(
                    "Braintrust app_url must be an HTTPS URL without credentials"
                )
        if not self._injected and not (self._api_key or os.environ.get("BRAINTRUST_API_KEY")):
            raise ConfigurationError("Braintrust reporting requires BRAINTRUST_API_KEY")
        if self._sdk is None:
            try:
                self._sdk = importlib.import_module("braintrust")
            except ImportError as exc:
                raise ConfigurationError(
                    "Braintrust reporting requires the optional package: pip install mic-evals[braintrust]"
                ) from exc
        if not callable(getattr(self._sdk, "init", None)):
            raise ConfigurationError("Braintrust SDK must expose init()")

    @asynccontextmanager
    async def open(self, run: RunInfo) -> AsyncGenerator["_Session"]:
        await self.prepare()
        session = _Session(self, run, self._sdk, self._injected, self._api_key)
        try:
            yield session
        finally:
            await session.pool.close()


class _Session:
    def __init__(
        self, sink: BraintrustSink, run: RunInfo, sdk: Any, injected: bool, api_key: str | None
    ) -> None:
        self.sink = sink
        self.run = run
        self.pool = CallbackPool(1)
        self.experiment: Any = None
        self.rows = 0
        self._sdk = sdk
        self._injected = injected
        self._api_key = api_key

    def _initialize(self) -> None:
        sink = self.sink
        options: dict[str, Any] = {
            "project": sink.project,
            "experiment": sink.experiment or f"mic-{self.run.run_id}",
            "set_current": False,
            "update": False,
            "metadata": {"mic_run_id": self.run.run_id, "mic_schema_version": "mic-run-v3"},
        }
        for key, value in {
            "api_key": self._api_key or os.environ.get("BRAINTRUST_API_KEY"),
            "app_url": sink.app_url,
            "org_name": sink.org_name,
        }.items():
            if value is not None:
                options[key] = value
        if not self._injected:
            options["state"] = _synchronous_state(self._sdk)
        self.experiment = self._sdk.init(**options)

    async def write(self, event: RunEvent) -> None:
        if not isinstance(event, TrialFinished):
            return
        projection = _events(
            {
                "run_id": self.run.run_id,
                "dataset": {"source_id": event.source_id, "task": event.task},
            },
            [event.result],
        )[0]
        await self.pool.invoke(self._write, event, projection)

    def _write(self, event: TrialFinished, projection: JsonObject) -> None:
        if self.experiment is None:
            self._initialize()
        experiment = cast(Any, self.experiment)
        span = experiment.start_span(
            name=f"{event.task} · {event.case_id} · trial {event.trial}",
            id=f"{event.case_id}:{event.task}:t{event.trial}",
            set_current=False,
        )
        try:
            span.log(**projection)
        finally:
            span.end()
        # One bounded trial at a time. Do not retry a failed/uncertain write.
        experiment.flush()
        self.rows += 1

    async def finish(self, outcome: EvaluationOutcome) -> SinkReceipt:
        details: JsonObject = {"rows": self.rows}
        if self.experiment is not None:
            summary = await self.pool.invoke(
                lambda: self.experiment.summarize(summarize_scores=False)
            )
            url: object = getattr(summary, "experiment_url", None)
            if isinstance(url, str):
                details["url"] = url
            identifier: object = getattr(self.experiment, "id", None)
            if isinstance(identifier, str):
                details["experiment_id"] = identifier
        return SinkReceipt("braintrust", "completed", details=details)
