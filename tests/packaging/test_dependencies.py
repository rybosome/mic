"""Prove dependency declarations and execute the native core without site packages."""

import os
import subprocess
import sys
import tomllib
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]


def test_core_has_no_runtime_dependencies_and_extras_name_only_their_adapter():
    with (PROJECT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]
    assert project["dependencies"] == []
    assert project["optional-dependencies"] == {
        "bigquery": ["google-cloud-bigquery>=3.30,<4"],
        "braintrust": ["braintrust==0.39.0"],
        "pydantic": ["pydantic>=2.11,<3"],
    }


def test_native_core_runs_dataclasses_and_offline_report_without_site_packages(tmp_path):
    # Keep only this project's own distribution metadata available for run
    # provenance. No site-packages directory or third-party module is on sys.path.
    with (PROJECT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]
    metadata = tmp_path / f"mic_evals-{project['version']}.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: mic-evals\nVersion: {project['version']}\n",
        encoding="utf-8",
    )
    script = """
import sys
from dataclasses import dataclass
from pathlib import Path
import mic
import mic.providers

@dataclass
class Item:
    text: str

@mic.dataset(name="isolated", schema=mic.case_schema(input=Item, expected=str))
def cases():
    return [{"id":"one", "input":{"text":"working"}, "expected":"working"}]

@mic.scorer(name="exact")
def exact(ctx):
    return mic.Score("exact", float(ctx.output == ctx.require_expected()))

@mic.eval(name="isolated", dataset=cases, output=str, scorers=[exact])
def predict(ctx, value):
    return value.text

result = mic.run(predict, output=Path("result"))
assert result.exit_code == 0
assert result.manifest["scores"]["exact"]["mean"] == 1.0
assert (result.output_dir / "report.html").is_file()
assert not ({"pydantic", "google", "braintrust", "httpx", "requests"} & set(sys.modules))
print("zero-dependency native core passed")
"""
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": os.pathsep.join((str(PROJECT / "src"), str(tmp_path))),
            "PYTHONNOUSERSITE": "1",
        }
    )
    result = subprocess.run(
        [sys.executable, "-S", "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "zero-dependency native core passed"
