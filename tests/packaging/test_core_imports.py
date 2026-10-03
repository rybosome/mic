"""A runtime import guard complements the clean-wheel packaging acceptance."""

import subprocess
import sys
from pathlib import Path


def test_local_dataclass_run_and_report_never_import_third_party_packages(tmp_path):
    root = Path(__file__).resolve().parents[2]
    script = r"""
import importlib.abc
import importlib.machinery
import json
from pathlib import Path
import sys

class StandardLibraryOnly(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        top = fullname.partition(".")[0]
        if (
            top not in sys.stdlib_module_names
            and top not in {"mic", "examples"}
            and importlib.machinery.PathFinder.find_spec(fullname, path) is not None
        ):
            raise AssertionError("Unexpected third-party import: " + fullname)

sys.meta_path.insert(0, StandardLibraryOnly())
root, output = map(Path, sys.argv[1:])
sys.path[:0] = [str(root / "src"), str(root)]
import mic
from examples.structured import classify, classify_file
from mic.reporters.html import write_report

memory = mic.run(classify, output=output / "memory")
file = mic.run(classify_file, output=output / "file")
assert memory.exit_code == file.exit_code == 0
assert next(iter(memory.sources.values())).digest == next(iter(file.sources.values())).digest
assert write_report(file.output_dir, output=output / "rerendered.html").exists()
print(json.dumps({"status": "passed", "completed": file.summary.trials.completed}))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script, str(root), str(tmp_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
