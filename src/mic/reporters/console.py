"""Compact console view of the public streaming result."""

from .._runtime.validation import json_object
from ..models import JsonObject


def format_summary(manifest: JsonObject) -> str:
    summary = json_object(manifest["summary"])
    trials = json_object(summary["trials"])
    lines = [
        f"Run          {manifest['run_id']}",
        f"Status       {manifest['status']}",
        f"Trials       {trials['completed']} completed / {trials['planned']} admitted",
        f"Failures     {trials['task_failed']} task, {trials['scoring_failed']} scoring",
    ]
    for task, value in json_object(summary["tasks"]).items():
        for metric, stats in json_object(json_object(value)["scores"]).items():
            detail = json_object(stats)
            lines.append(f"{task}.{metric}: mean={detail['mean']} count={detail['count']}")
    return "\n".join(lines)
