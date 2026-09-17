"""Local evidence is written before optional remote reporting."""

import hashlib
import importlib.metadata
import platform
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from ..errors import ConfigurationError
from ..models import JsonObject, JsonValue
from .validation import dumps, json_object


def atomic_json(path: Path, value: JsonObject) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(dumps(value) + "\n", encoding="utf-8")
    temporary.replace(path)


def prepare_directory(path: Path) -> None:
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ConfigurationError(f"Output directory must be empty: {path}")
    path.mkdir(parents=True, exist_ok=True)


def write_dataset(path: Path, rows: Sequence[JsonObject]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(dumps(row) + "\n")


def append_case(path: Path, case: JsonObject) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(dumps(case) + "\n")


def finish_artifacts(path: Path, manifest: JsonObject, cases: Sequence[JsonObject]) -> None:
    from ..reporters.html import render_report

    atomic_json(path / "run.json", manifest)
    report = path / "report.html"
    temporary = report.with_name("report.html.tmp")
    temporary.write_text(render_report(manifest, cases), encoding="utf-8")
    temporary.replace(report)


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
    package = Path(__file__).parent
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
