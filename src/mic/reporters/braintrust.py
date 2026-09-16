"""Opt-in Braintrust export. Never delegates execution to Braintrust Eval."""

import asyncio
import copy
import importlib
import json
import math
import os
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse

from mic._async import drain
from mic.errors import ConfigurationError
from mic.models import JsonObject


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
    # The reporter flushes bounded batches. A small SDK queue must not drop spans
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


class BraintrustReporter:
    """Explicit experiment sink with a lazy optional SDK and injectable test boundary.

    ``prepare`` verifies local configuration only; it cannot prove remote permissions.
    Cloud access occurs only in ``report`` after the core has saved local results.
    """

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
        self._state: Any = None

    async def prepare(self) -> None:
        if not self.project.strip():
            raise ConfigurationError("Braintrust reporter requires a nonempty project")
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

    async def report(self, manifest: JsonObject, cases: Sequence[JsonObject]) -> JsonObject:
        await self.prepare()
        events = _events(manifest, cases)
        upload = asyncio.create_task(asyncio.to_thread(self._upload, manifest, cases, events))
        try:
            return await asyncio.shield(upload)
        except asyncio.CancelledError:
            # Python cannot stop an active SDK thread. Drain it before returning
            # cancellation, so no hidden export continues after the run returns.
            # The remote experiment may already contain all or part of the run.
            try:
                await drain(upload)
            except Exception:
                pass
            raise

    def _upload(
        self, manifest: JsonObject, cases: Sequence[JsonObject], events: list[JsonObject]
    ) -> JsonObject:
        options: dict[str, Any] = {
            "project": self.project,
            "experiment": self.experiment or f"{manifest.get('name')}-{manifest.get('run_id')}",
            "set_current": False,
            "update": False,
            "metadata": {
                "mic_run_id": manifest.get("run_id"),
                "mic_schema_version": manifest.get("schema_version"),
            },
        }
        for key, value in {
            "api_key": self._api_key or os.environ.get("BRAINTRUST_API_KEY"),
            "app_url": self.app_url,
            "org_name": self.org_name,
        }.items():
            if value is not None:
                options[key] = value
        if not self._injected:
            if self._state is None:
                self._state = _synchronous_state(self._sdk)
            options["state"] = self._state
        experiment = self._sdk.init(**options)
        try:
            for index, (case, event) in enumerate(zip(cases, events, strict=True), 1):
                span = experiment.start_span(
                    name=f"{case.get('case_id')} · trial {case.get('trial')}",
                    id=f"{manifest.get('run_id')}:{case.get('row_index')}:{case.get('trial')}",
                    set_current=False,
                )
                try:
                    span.log(**event)
                except Exception as exc:
                    try:
                        span.end()
                    except Exception as end_error:
                        exc.add_note(f"Ending Braintrust span also failed: {end_error}")
                    raise
                else:
                    span.end()
                if index % 100 == 0:
                    experiment.flush()
            experiment.flush()
        except Exception as exc:
            # Drain any earlier queued spans before returning a partial-export failure.
            # Otherwise the SDK's process-exit hook could upload them later.
            try:
                experiment.flush()
            except Exception as flush_error:
                exc.add_note(f"Final Braintrust flush also failed: {flush_error}")
            raise
        summary = experiment.summarize(summarize_scores=False)
        result: JsonObject = {"status": "completed", "rows": len(events), "flushed": True}
        url: object = getattr(summary, "experiment_url", None)
        if isinstance(url, str):
            result["url"] = url
        experiment_id: object = getattr(experiment, "id", None)
        if isinstance(experiment_id, str):
            result["experiment_id"] = experiment_id
        return result
