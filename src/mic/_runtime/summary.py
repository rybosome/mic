"""Run aggregates and explicit numeric quality gates."""

import math
import operator
import re
from collections.abc import Callable, Sequence
from typing import cast

from ..errors import ConfigurationError
from ..models import Evaluation, JsonObject, JsonValue
from .validation import json_object, numeric_stats


def summarize[I, O, E, M](
    spec: Evaluation[I, O, E, M],
    manifest: JsonObject,
    cases: Sequence[JsonObject],
    planned: int,
    *,
    cancelled: bool = False,
) -> None:
    completed = sum(case["status"] == "completed" for case in cases)
    failed = sum(case["status"] == "failed" for case in cases)
    cancelled_count = (
        planned - completed - failed
        if cancelled
        else sum(case["status"] == "cancelled" for case in cases)
    )
    manifest["counts"] = {
        "planned": planned,
        "completed": completed,
        "failed": failed,
        "cancelled": cancelled_count,
        "skipped": 0,
    }
    aggregates: JsonObject = {}
    for metric in sorted(metric for scorer in spec.scorers for metric in scorer.metrics):
        values: list[float] = []
        null_count = 0
        for case in cases:
            for value in cast(list[JsonValue], case.get("scores", [])):
                score = json_object(value)
                if score["name"] == metric:
                    if score["value"] is None:
                        null_count += 1
                    else:
                        values.append(float(cast(float, score["value"])))
        aggregates[metric] = {
            **numeric_stats(values),
            "null_count": null_count,
            "unavailable_count": planned - len(values) - null_count,
        }
    manifest["scores"] = aggregates
    latency: JsonObject = {}
    for key in ("task_ms", "scoring_ms", "total_ms"):
        latency[key] = numeric_stats(
            [float(cast(float, json_object(case["latency"])[key])) for case in cases]
        )
    manifest["latency"] = latency


_GATE = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_. /-]*?)\s*(>=|<=|==|>|<)\s*(-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)$"
)
_COMPARATORS: dict[str, Callable[[float, float], bool]] = {
    ">=": operator.ge,
    "<=": operator.le,
    "==": operator.eq,
    ">": operator.gt,
    "<": operator.lt,
}


def parse_gates[I, O, E, M](
    spec: Evaluation[I, O, E, M], expressions: Sequence[str]
) -> list[tuple[str, str, str, float]]:
    allowed = {metric for scorer in spec.scorers for metric in scorer.metrics}
    parsed: list[tuple[str, str, str, float]] = []
    for expression in expressions:
        match = _GATE.fullmatch(expression.strip())
        if match is None:
            raise ConfigurationError(f"Invalid score gate {expression!r}; use 'metric>=0.9'")
        metric, comparison, bound = match.groups()
        metric = metric.strip()
        threshold = float(bound)
        if metric not in allowed:
            raise ConfigurationError(f"Gate references undeclared metric {metric!r}")
        if not math.isfinite(threshold):
            raise ConfigurationError("Gate threshold must be finite")
        parsed.append((expression, metric, comparison, threshold))
    return parsed


def evaluate_gates(manifest: JsonObject, parsed: Sequence[tuple[str, str, str, float]]) -> bool:
    gates: list[JsonValue] = []
    score_summary = json_object(manifest["scores"])
    successful = True
    for expression, metric, comparison, threshold in parsed:
        actual = json_object(score_summary[metric])["mean"]
        passed = actual is not None and _COMPARATORS[comparison](
            float(cast(float, actual)), threshold
        )
        gates.append(
            {"expression": expression, "metric": metric, "actual": actual, "passed": passed}
        )
        successful = successful and passed
    manifest["gates"] = gates
    return successful
