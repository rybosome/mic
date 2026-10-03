"""Dependency-free structural checks for the fields consumed by the report UI."""

from .._runtime.validation import json_object
from ..models import JsonObject, JsonValue


def _text(value: JsonValue) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Expected nonempty text")


def _count(value: JsonValue, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError("Expected a nonnegative integer")


def _array(value: JsonValue) -> list[JsonValue]:
    if not isinstance(value, list):
        raise ValueError("Expected an array")
    return value


def _error(value: JsonValue) -> None:
    error = json_object(value)
    for field in ("phase", "type", "message"):
        _text(error[field])


def _statistics(value: JsonValue) -> None:
    stats = json_object(value)
    _count(stats["count"])
    for field in ("mean", "min", "max"):
        number = stats[field]
        if number is not None and (
            isinstance(number, bool) or not isinstance(number, (float, int))
        ):
            raise ValueError("Invalid statistic")
        if (stats["count"] == 0) != (number is None):
            raise ValueError("Empty statistics must have null values")


def _trials(value: JsonValue) -> None:
    trials = json_object(value)
    for field in (
        "planned",
        "completed",
        "task_failed",
        "scoring_failed",
        "scoring_skipped",
        "cancelled",
    ):
        _count(trials[field])
    for field in ("task_ms", "scoring_ms", "total_ms"):
        _statistics(trials[field])


def _source(value: JsonValue) -> None:
    source = json_object(value)
    _text(source["name"])
    for field in ("records_seen", "records_accepted", "records_rejected"):
        _count(source[field])
    if type(source["exhausted"]) is not bool:
        raise ValueError("Invalid source exhaustion state")
    json_object(source["provenance"])
    if source["error"] is not None:
        _error(source["error"])


def check_manifest(manifest: JsonObject) -> None:
    _text(manifest["run_id"])
    summary = json_object(manifest["summary"])
    _trials(summary["trials"])
    for name, item in json_object(summary["tasks"]).items():
        _text(name)
        task = json_object(item)
        _trials(task["trials"])
        for metric, stats in json_object(task["scores"]).items():
            _text(metric)
            _statistics(stats)
    for value in json_object(manifest["sources"]).values():
        _source(value)
    for value in _array(manifest["requirements"]):
        requirement = json_object(value)
        _text(requirement["expression"])
        if type(requirement["passed"]) is not bool:
            raise ValueError("Invalid requirement outcome")
    for value in _array(manifest["failures"]):
        _error(value)
    for value in _array(manifest["sinks"]):
        receipt = json_object(value)
        _text(receipt["name"])
        if receipt["status"] not in ("completed", "failed", "cancelled"):
            raise ValueError("Invalid sink status")
        if receipt["error"] is not None:
            _error(receipt["error"])


def check_event(event: JsonObject) -> JsonObject | None:
    """Validate the envelope; return a trial projection only for trial events."""
    _text(event["source_id"])
    kind = event["type"]
    if kind == "source_finished":
        _source(event["summary"])
        return None
    _count(event["row_index"])
    if kind == "record_rejected":
        _error(event["error"])
        return None
    _text(event["case_id"])
    if kind == "case_accepted":
        case = json_object(event["case"])
        if "input" not in case:
            raise ValueError("Missing case input")
        return None
    if kind != "trial_finished":
        raise ValueError("Unknown event")
    _text(event["task"])
    _count(event["trial"], 1)
    result = json_object(event["result"])
    if "input" not in result:
        raise ValueError("Missing trial input")
    for field in ("row_index", "trial", "case_id"):
        if result[field] != event[field]:
            raise ValueError("Trial coordinates disagree")
    if result["status"] not in ("completed", "task_failed", "scoring_failed", "cancelled"):
        raise ValueError("Invalid trial status")
    if result["status"] in ("completed", "scoring_failed") and "output" not in result:
        raise ValueError("Missing validated output")
    for value in _array(result["scores"]):
        score = json_object(value)
        _text(score["name"])
        number = score["value"]
        if number is not None and (
            isinstance(number, bool) or not isinstance(number, (float, int))
        ):
            raise ValueError("Invalid score")
    for value in _array(result["errors"]):
        _error(value)
    latency = json_object(result["latency"])
    for field in ("task_ms", "scoring_ms", "total_ms"):
        number = latency[field]
        if number is not None and (
            isinstance(number, bool) or not isinstance(number, (float, int)) or number < 0
        ):
            raise ValueError("Invalid phase timing")
    result.update({"task": event["task"], "source_id": event["source_id"]})
    return result
