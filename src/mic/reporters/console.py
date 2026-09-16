"""Plain-text summaries suitable for terminals, logs, and CI."""

from mic.models import JsonObject, JsonValue


def _object(value: JsonValue) -> JsonObject:
    return value if isinstance(value, dict) else {}


def format_summary(manifest: JsonObject) -> str:
    """Keep execution status separate from score quality and explicit gates."""
    counts = _object(manifest.get("counts"))
    dataset = _object(manifest.get("dataset"))
    options = _object(manifest.get("options"))
    lines = [
        str(manifest.get("name", "mic run")),
        f"Status       {manifest.get('status', 'unknown')}",
        f"Source       {dataset.get('name', 'unknown')} · {dataset.get('rows', 0)} rows",
        f"Snapshot     {dataset.get('digest', 'unavailable')}",
        f"Execution    {counts.get('completed', 0)} completed / {counts.get('planned', 0)} "
        f"planned · {counts.get('failed', 0)} errors · {counts.get('cancelled', 0)} cancelled",
        f"Options      {options.get('trials', 1)} trial(s) · "
        f"concurrency {options.get('concurrency', 1)}",
    ]
    for name, value in _object(manifest.get("scores")).items():
        stats = _object(value)
        mean = stats.get("mean")
        display = f"{mean:.3f}" if isinstance(mean, (float, int)) else "unscored"
        lines.append(
            f"Score        {name} {display} · numeric {stats.get('count', 0)} · "
            f"unscored {stats.get('null_count', 0)} · "
            f"unavailable {stats.get('unavailable_count', 0)}"
        )
    gates = manifest.get("gates")
    if isinstance(gates, list) and gates:
        for value in gates:
            gate = _object(value)
            lines.append(
                f"Quality      {'PASS' if gate.get('passed') else 'FAIL'} "
                f"{gate.get('expression')} (actual {gate.get('actual')})"
            )
    else:
        lines.append("Quality      no gate configured")
    for name, value in _object(manifest.get("reporting")).items():
        status = _object(value)
        lines.append(f"Reporting    {name}: {status.get('status')} {status.get('url', '')}")
    return "\n".join(lines)
