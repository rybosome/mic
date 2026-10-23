"""Compact console view of the public streaming result."""

from typing import cast

from .._runtime.validation import json_object
from ..models import JsonObject, JsonValue
from .live import duration


def _mean(value: JsonValue) -> str:
    """Round for reading; exact values stay in --json and run.json."""
    if not isinstance(value, int | float) or isinstance(value, bool):
        return "n/a"
    if value == 0 or 0.001 <= abs(value) < 1e6:
        return f"{value:.3f}"
    return f"{value:.3e}"


def format_summary(manifest: JsonObject, *, elapsed: float | None = None) -> str:
    summary = json_object(manifest["summary"])
    trials = json_object(summary["trials"])
    lines = [
        f"Run          {manifest['run_id']}",
        f"Status       {manifest['status']}",
    ]
    if elapsed is not None:
        lines.append(f"Elapsed      {duration(elapsed)}")
    lines += [
        f"Trials       {trials['completed']} completed / {trials['planned']} admitted",
        f"Failures     {trials['task_failed']} task, {trials['scoring_failed']} scoring",
    ]
    scores: list[tuple[str, str, str]] = []
    for task, value in json_object(summary["tasks"]).items():
        for metric, stats in json_object(json_object(value)["scores"]).items():
            detail = json_object(stats)
            scores.append((f"{task}.{metric}", _mean(detail["mean"]), str(detail["count"])))
    if scores:
        name_width = max(len(name) for name, _, _ in scores)
        mean_width = max(len(mean) for _, mean, _ in scores)
        lines += [
            f"Score        {name:<{name_width}}  mean {mean:>{mean_width}}  count {count}"
            for name, mean, count in scores
        ]
    # Requirement actuals stay exact so a rounded value never contradicts PASS/FAIL.
    for value in cast(list[JsonValue], manifest["requirements"]):
        requirement = json_object(value)
        outcome = "PASS" if requirement["passed"] else "FAIL"
        lines.append(
            f"Requirement  {outcome} {requirement['expression']} (actual: {requirement['actual']})"
        )
    for name, value in json_object(manifest["sources"]).items():
        source = json_object(value)
        lines.append(
            f"Source       {source['name']}: {source['records_accepted']} accepted, {source['records_rejected']} rejected"
        )
        if source["error"] is not None:
            lines.append(f"Source error {name}: {json_object(source['error'])['message']}")
        elif not source["exhausted"]:
            lines.append(f"Source       {name}: incomplete prefix")
    for value in cast(list[JsonValue], manifest["failures"]):
        error = json_object(value)
        lines.append(f"Run error    {error['phase']}: {error['message']}")
    for value in cast(list[JsonValue], manifest["sinks"]):
        sink = json_object(value)
        if sink["status"] != "completed":
            lines.append(f"Sink         {sink['name']}: {sink['status']}")
    return "\n".join(lines)
