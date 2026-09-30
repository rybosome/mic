"""Validate reporters and export completed results after local evidence exists."""

import asyncio
import copy
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from ..errors import ConfigurationError
from ..models import JsonObject, JsonValue
from ..reporters.base import Reporter
from .artifacts import failure, finish_artifacts
from .validation import json_object, nonempty


async def prepare_reporters(reporters: Sequence[Reporter]) -> None:
    names: set[str] = set()
    for reporter in reporters:
        name = nonempty("reporter name", reporter.name)
        if name in names:
            raise ConfigurationError(f"Duplicate reporter {name!r}")
        names.add(name)
        try:
            await reporter.prepare()
        except ConfigurationError:
            raise
        except Exception as exc:
            raise ConfigurationError(f"Reporter {name!r} could not prepare: {exc}") from exc


async def export_results(
    reporters: Sequence[Reporter],
    manifest: JsonObject,
    cases: Sequence[JsonObject],
    destination: Path,
    exit_code: int,
) -> int:
    report_results: JsonObject = {}
    manifest["reporting"] = report_results
    for reporter in reporters:
        try:
            report_results[reporter.name] = json_object(
                await reporter.report(copy.deepcopy(manifest), copy.deepcopy(cases))
            )
        except asyncio.CancelledError as exc:
            report_results[reporter.name] = {
                "status": "cancelled",
                "error": "Upload cancelled; local results are complete",
            }
            manifest.update({"status": "cancelled", "exit_code": 130})
            cast(list[JsonValue], manifest["failures"]).append(failure("reporting", exc))
            finish_artifacts(destination, manifest, cases)
            raise
        except Exception as exc:
            report_results[reporter.name] = {
                "status": "failed",
                "error": str(exc),
                "type": type(exc).__name__,
            }
            exit_code = 1
            manifest["status"] = "failed"
            manifest["exit_code"] = exit_code
        finish_artifacts(destination, manifest, cases)
    return exit_code
