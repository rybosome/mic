"""Render portable, offline HTML directly from the authoritative local artifacts."""

import base64
import hashlib
import html
import json
import os
import re
import tempfile
from collections.abc import Sequence
from pathlib import Path

from mic._runtime.validation import json_object, loads
from mic.errors import ConfigurationError
from mic.models import JsonObject

_TEMPLATES = Path(__file__).parent / "templates"


def _write_atomic(destination: Path, rendered: str) -> None:
    """Keep a previous report intact if a replacement cannot be written."""
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=destination.parent, prefix=".mic-report-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(rendered)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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
        "__TITLE__": html.escape(str(manifest.get("name", "mic run")), quote=True),
        "__DATA__": payload,
    }
    # A single pass keeps literal template markers in user titles/data unchanged.
    return re.sub(
        r"__SCRIPT_HASH__|__SCRIPT__|__TITLE__|__DATA__",
        lambda match: replacements[match[0]],
        template,
    )


def write_report(run_path: Path, output: Path | None = None) -> Path:
    """Re-render a saved run without definitions, optional SDKs, or task execution."""
    run_path = Path(run_path)
    if run_path.is_dir():
        run_path /= "run.json"
    try:
        destination = Path(output) if output is not None else run_path.parent / "report.html"
        artifacts = [run_path, run_path.parent / "cases.jsonl", run_path.parent / "dataset.jsonl"]
        for artifact in artifacts:
            if destination.resolve() == artifact.resolve() or (
                destination.exists() and artifact.exists() and destination.samefile(artifact)
            ):
                raise ConfigurationError(
                    f"Report output would overwrite source artifact {artifact}"
                )
        manifest = json_object(loads(run_path.read_text(encoding="utf-8")))
        if manifest.get("schema_version") != "mic-run-v1":
            raise ConfigurationError("Unsupported run artifact schema; expected mic-run-v1")
        cases_path = run_path.parent / "cases.jsonl"
        cases: list[JsonObject] = []
        with cases_path.open(encoding="utf-8") as stream:
            for number, line in enumerate(stream, 1):
                if line.strip():
                    try:
                        cases.append(json_object(loads(line)))
                    except (TypeError, ValueError) as exc:
                        raise ConfigurationError(
                            f"Invalid case artifact at {cases_path}:{number}: {exc}"
                        ) from exc
        rendered = render_report(manifest, cases)
        destination.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(destination, rendered)
        return destination
    except (OSError, TypeError, ValueError) as exc:
        raise ConfigurationError(f"Cannot render report from {run_path}: {exc}") from exc
