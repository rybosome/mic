"""Render portable, offline HTML directly from the authoritative local artifacts."""

import base64
import hashlib
import html
import json
import re
from collections.abc import Sequence
from pathlib import Path

from mic._runtime.files import atomic_write
from mic._runtime.validation import json_object, loads, positive_integer
from mic.errors import ConfigurationError
from mic.models import JsonObject

from ._data import check_event, check_manifest

_TEMPLATES = Path(__file__).parent / "templates"


def render_report(manifest: JsonObject, cases: Sequence[JsonObject]) -> str:
    """Escape embedded data and use DOM text nodes; no dataset content becomes HTML."""
    script = (_TEMPLATES / "report.js").read_text(encoding="utf-8").strip()
    script_hash = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    payload = json.dumps(
        {"manifest": manifest, "cases": list(cases)}, ensure_ascii=False, allow_nan=False
    )
    payload = (
        payload.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    template = (_TEMPLATES / "report.html").read_text(encoding="utf-8")
    replacements = {
        "__SCRIPT_HASH__": script_hash,
        "__SCRIPT__": script,
        "__TITLE__": html.escape("Mic evaluation", quote=True),
        "__DATA__": payload,
    }
    # A single pass keeps literal template markers in user titles/data unchanged.
    return re.sub(
        r"__SCRIPT_HASH__|__SCRIPT__|__TITLE__|__DATA__",
        lambda match: replacements[match[0]],
        template,
    )


def write_report(
    run_path: Path,
    output: Path | None = None,
    *,
    max_cases: int = 10_000,
    max_bytes: int = 64 * 1024 * 1024,
) -> Path:
    """Render only the current event format, with explicit in-memory report caps."""
    positive_integer("max_cases", max_cases)
    positive_integer("max_bytes", max_bytes)
    run_path = Path(run_path)
    if run_path.is_dir():
        run_path /= "run.json"
    try:
        destination = Path(output) if output is not None else run_path.parent / "report.html"
        events_path = run_path.parent / "events.jsonl"
        for artifact in (run_path, events_path):
            if destination.resolve() == artifact.resolve() or (
                destination.exists() and artifact.exists() and destination.samefile(artifact)
            ):
                raise ConfigurationError(
                    f"Report output would overwrite source artifact {artifact}"
                )
        with run_path.open("rb") as stream:
            data = stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ConfigurationError("Report exceeds max_bytes")
        manifest = json_object(loads(data))
        if manifest.get("schema_version") != "mic-run-v3":
            raise ConfigurationError("Unsupported run artifact schema; expected mic-run-v3")
        if manifest.get("status") not in ("completed", "failed", "cancelled"):
            raise ConfigurationError("Run evidence has not been finalized")
        check_manifest(manifest)
        cases: list[JsonObject] = []
        consumed = len(data)
        with events_path.open("rb") as stream:
            while line := stream.readline(max_bytes - consumed + 1):
                consumed += len(line)
                if consumed > max_bytes:
                    raise ConfigurationError("Report exceeds max_bytes")
                event = json_object(loads(line))
                if event.get("schema_version") != "mic-event-v1":
                    raise ConfigurationError(
                        "Unsupported event artifact schema; expected mic-event-v1"
                    )
                result = check_event(event)
                if result is not None:
                    if len(cases) >= max_cases:
                        raise ConfigurationError("Report exceeds max_cases")
                    cases.append(result)
        rendered = render_report(manifest, cases)
        destination.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(destination, (rendered,))
        return destination
    except ConfigurationError:
        raise
    except (OSError, TypeError, ValueError, KeyError):
        raise ConfigurationError("Cannot render recorded evaluation evidence") from None
