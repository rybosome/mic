"""Local evidence is written before optional remote reporting."""

import hashlib
import importlib.metadata
import platform
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from ..errors import ConfigurationError
from ..models import JsonObject, JsonValue
from .files import atomic_write
from .validation import dumps, json_object


def atomic_json(path: Path, value: JsonObject) -> None:
    atomic_write(path, (dumps(value) + "\n",))


def prepare_directory(path: Path) -> None:
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ConfigurationError(f"Output directory must be empty: {path}")
    path.mkdir(parents=True, exist_ok=True)


def write_dataset(path: Path, rows: Sequence[JsonObject]) -> None:
    atomic_write(path, (dumps(row) + "\n" for row in rows))


def append_case(path: Path, case: JsonObject) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(dumps(case) + "\n")


@dataclass
class Persistence:
    """Successful writes and all failed attempts, without claiming disk recovery."""

    written: set[str] = field(default_factory=set[str])
    errors: list[tuple[str, Exception]] = field(default_factory=list[tuple[str, Exception]])

    def annotate(self, exc: BaseException, path: Path) -> None:
        if self.errors:
            exc.add_note(f"Local evidence in {path} may be incomplete or stale")
            for name, error in self.errors:
                exc.add_note(f"Could not persist {name}: {type(error).__name__}: {error}")
        elif "report.html" in self.written:
            exc.add_note(f"Local evidence: {path / 'report.html'}")


def has_artifact_failure(manifest: JsonObject) -> bool:
    return any(
        isinstance(error, dict) and error.get("phase") == "artifact"
        for error in cast(list[JsonValue], manifest["failures"])
    )


def end_run(manifest: JsonObject, *, started: float) -> None:
    """Stamp terminal timing even when no output directory could be acquired."""
    manifest["ended_at"] = datetime.now(UTC).isoformat()
    manifest["duration_ms"] = (time.perf_counter() - started) * 1000


def finish_artifacts(
    path: Path, manifest: JsonObject, cases: Sequence[JsonObject], *, started: float
) -> Persistence:
    """Save a terminal outcome and attempt one bounded recovery after write failures.

    Both destinations are attempted independently. Recovery reprojects the updated
    failure manifest; persistent faults may leave disk behind the in-memory result.
    Setup errors (2) and cancellation (130) retain their original precedence.
    """
    from ..reporters.html import render_report

    end_run(manifest, started=started)
    result = Persistence()
    for _ in range(2):
        errors: list[tuple[str, Exception]] = []
        for name in ("run.json", "report.html"):
            try:
                if name == "run.json":
                    atomic_json(path / name, manifest)
                else:
                    atomic_write(path / name, (render_report(manifest, cases),))
                result.written.add(name)
            except Exception as exc:
                # Rendering is an artifact boundary too. BaseException (including
                # cancellation) deliberately remains outside this recovery path.
                errors.append((name, exc))
                result.written.discard(name)
        if not errors:
            break
        result.errors.extend(errors)
        for name, exc in errors:
            record = failure("artifact", exc)
            record["message"] = f"Could not persist {name}: {exc}"
            cast(list[JsonValue], manifest["failures"]).append(record)
        if manifest["exit_code"] not in (2, 130):
            manifest.update({"status": "failed", "exit_code": 1})
    return result


def code_provenance(fn: object) -> JsonObject:
    module_name = str(getattr(fn, "__module__", "unknown"))
    symbol = str(getattr(fn, "__qualname__", getattr(fn, "__name__", "unknown")))
    try:
        package_version = importlib.metadata.version("mic-evals")
    except importlib.metadata.PackageNotFoundError:
        package_version = "uninstalled"
    metadata: JsonObject = {
        "python": platform.python_version(),
        "package_version": package_version,
        "definition": f"{module_name}:{symbol}",
        "dependencies": {},
    }
    # Record optional schema/backend versions only when they were actually loaded.
    dependencies: JsonObject = {}
    for distribution, module_prefix in (("pydantic", "pydantic"),):
        if module_prefix in sys.modules:
            try:
                dependencies[distribution] = importlib.metadata.version(distribution)
            except importlib.metadata.PackageNotFoundError:
                pass
    metadata["dependencies"] = dependencies
    module = sys.modules.get(module_name)
    module_file = getattr(module, "__file__", None)
    if module_file is not None:
        source_path = Path(module_file).resolve()
        if source_path.is_file():
            metadata["source_path"] = str(source_path)
            metadata["source_sha256"] = hashlib.sha256(source_path.read_bytes()).hexdigest()
    # A framework content hash also works outside Git and is meaningful for an
    # editable install with uncommitted changes.
    package = Path(__file__).parents[1]
    hasher = hashlib.sha256()
    for source in sorted(package.rglob("*.py")):
        hasher.update(str(source.relative_to(package)).encode())
        hasher.update(source.read_bytes())
    metadata["framework_sha256"] = hasher.hexdigest()
    return metadata


def failure(
    phase: str,
    exc: BaseException,
    *,
    row_index: int | None = None,
    case_id: str | None = None,
    trial: int | None = None,
    scorer: str | None = None,
) -> JsonObject:
    import traceback

    result: JsonObject = {
        "phase": phase,
        "type": type(exc).__name__,
        "message": str(exc),
        "row_index": row_index,
        "case_id": case_id,
        "trial": trial,
        "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
    }
    if scorer is not None:
        result["scorer"] = scorer
    return result


def case_errors(cases: Sequence[JsonObject]) -> list[JsonValue]:
    errors: list[JsonValue] = []
    for case in cases:
        for error in cast(list[JsonValue], case.get("errors", [])):
            errors.append(json_object(error))
    return errors
