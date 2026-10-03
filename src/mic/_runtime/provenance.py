"""Code/environment identity without reading secrets or contacting services."""

import hashlib
import importlib.metadata
import platform
import sys
from pathlib import Path

from ..models import JsonObject


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
