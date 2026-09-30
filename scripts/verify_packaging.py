"""Verify a built wheel in a fresh environment with no third-party dependencies.

Run ``uv build`` first, then ``python scripts/verify_packaging.py``. This verifier
uses only the standard library; uv is needed solely to install the local wheel
with --no-deps and --offline. Generated environments and run artifacts stay in
.cache; generated verification output is written under .artifacts by default.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import venv
import zipfile
from datetime import UTC, datetime
from email.parser import Parser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSPECT_INSTALL = """import importlib.util, importlib.metadata, json, mic, sys

def present(name):
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False

print(json.dumps({
    "mic_path": mic.__file__, "python": sys.version,
    "optional_packages": {
        name: present(name)
        for name in ("pydantic", "pydantic_core", "httpx", "requests", "braintrust", "google.cloud.bigquery")
    },
    "distributions": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()},
}))
"""


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_archives(wheel, sdist):
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        package_sources = {
            str(path.relative_to(ROOT / "src")): path
            for path in (ROOT / "src/mic").rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
        }
        package_members = {
            name for name in names if name.startswith("mic/") and not name.endswith("/")
        }
        assert package_members == package_sources.keys(), (
            f"Wheel package topology differs: extra={package_members - package_sources.keys()}, "
            f"missing={package_sources.keys() - package_members}"
        )
        assert "mic/reporters/templates/report.html" in names, "HTML template missing from wheel"
        assert "mic/reporters/templates/report.js" in names, "Report JavaScript missing from wheel"
        for member, path in package_sources.items():
            assert archive.read(member) == path.read_bytes(), f"Wheel differs from source {path}"
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata_text = archive.read(metadata_name).decode("utf-8")
        metadata = Parser().parsestr(metadata_text)
        assert metadata["License-Expression"] == "MIT", "Wheel license metadata is not MIT"
        assert any(name.endswith(".dist-info/licenses/LICENSE") for name in names), (
            "MIT license text missing from wheel"
        )
        assert (
            metadata.get_payload().strip()
            == (ROOT / "README.md").read_text(encoding="utf-8").strip()
        ), "Wheel README differs from source"
        requirements = metadata.get_all("Requires-Dist", [])
        mandatory = [requirement for requirement in requirements if "; extra ==" not in requirement]
        assert not mandatory, f"Core wheel still declares required dependencies: {mandatory}"
    with tarfile.open(sdist) as archive:
        members = archive.getnames()
        excluded = (
            "/.cache/",
            "/.venv/",
            "/.mic/",
            "/.artifacts/",
            "/__pycache__/",
            "/node_modules/",
        )
        assert not any(part in member for member in members for part in excluded), (
            "Source distribution contains generated environments or run data"
        )
        for member in archive.getmembers():
            if member.isfile():
                local = ROOT / Path(*Path(member.name).parts[1:])
                if local.is_file():
                    assert archive.extractfile(member).read() == local.read_bytes(), (
                        f"Source archive differs from source {local}"
                    )
        relative_members = {str(Path(*Path(member).parts[1:])) for member in members}
        for path in (ROOT / "docs/artifact-schemas").glob("*.json"):
            assert str(path.relative_to(ROOT)) in relative_members, f"Schema missing: {path}"
        assert "docs/artifacts.md" in relative_members, "Artifact reference missing from sdist"
    files = sorted(
        [ROOT / "LICENSE", ROOT / "README.md", ROOT / "pyproject.toml"]
        + [
            path
            for path in (ROOT / "src").rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
        ]
    )
    sources = [{"path": str(path.relative_to(ROOT)), "sha256": sha256(path)} for path in files]
    fingerprint = hashlib.sha256(
        json.dumps(sources, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "wheel": {
            "path": str(wheel),
            "sha256": sha256(wheel),
            "bytes": wheel.stat().st_size,
            "members": names,
            "metadata": metadata_text,
            "required_dependencies": mandatory,
            "included_sources_match": True,
            "package_topology_matches": True,
        },
        "sdist": {
            "path": str(sdist),
            "sha256": sha256(sdist),
            "bytes": sdist.stat().st_size,
            "generated_data_excluded": True,
            "included_sources_match": True,
        },
        "source_fingerprint": {
            "algorithm": "sha256 of sorted path/content-sha256 JSON",
            "sha256": fingerprint,
            "files": sources,
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--sdist", type=Path)
    parser.add_argument(
        "--output", type=Path, default=ROOT / ".artifacts/packaging/verification.json"
    )
    args = parser.parse_args()
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    stem = f"{project['name'].replace('-', '_')}-{project['version']}"
    wheel = (args.wheel or ROOT / "dist" / f"{stem}-py3-none-any.whl").resolve()
    sdist = (args.sdist or ROOT / "dist" / f"{stem}.tar.gz").resolve()
    cache = ROOT / ".cache"
    cache.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="zero-dependency-wheel-", dir=cache))
    isolated = work / "venv"
    steps = []
    evidence = {
        "verified_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "verifier": "scripts/verify_packaging.py",
        "work_directory": str(work),
        "steps": steps,
        "limitations": [
            "No authenticated provider checks",
            "HTML generation and artifact fidelity verified; no interactive browser review",
        ],
    }
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["UV_CACHE_DIR"] = str(cache / "uv")

    def run(name, command, *, cwd=ROOT):
        command = [str(part) for part in command]
        started = time.monotonic()
        result = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        steps.append(
            {
                "name": name,
                "command": command,
                "cwd": str(cwd),
                "exit_code": result.returncode,
                "duration_seconds": round(time.monotonic() - started, 3),
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
        assert result.returncode == 0, f"{name} failed: {result.stderr or result.stdout}"
        return result.stdout

    try:
        assert project.get("dependencies", []) == [], "Core pyproject dependencies must be empty"
        evidence.update(verify_archives(wheel, sdist))
        venv.EnvBuilder(with_pip=False).create(isolated)
        python = isolated / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        mic = isolated / ("Scripts/mic.exe" if os.name == "nt" else "bin/mic")
        uv = shutil.which("uv")
        assert uv, "uv is required to install the local wheel"
        before = json.loads(
            run(
                "empty_environment",
                [
                    python,
                    "-c",
                    (
                        "import importlib.metadata,json; "
                        "print(json.dumps([d.metadata['Name'] for d in importlib.metadata.distributions()]))"
                    ),
                ],
                cwd=work,
            )
        )
        assert before == [], f"Fresh environment already contains distributions: {before}"
        run(
            "install_wheel_without_dependencies",
            [
                uv,
                "pip",
                "install",
                "--offline",
                "--no-deps",
                "--python",
                python,
                wheel,
            ],
        )
        installed = json.loads(
            run("inspect_installed_core", [python, "-c", INSPECT_INSTALL], cwd=work)
        )
        assert installed["distributions"] == {project["name"]: project["version"]}, (
            f"Unexpected installed distributions: {installed['distributions']}"
        )
        assert not any(installed["optional_packages"].values()), "Optional dependencies are present"
        assert Path(installed["mic_path"]).is_relative_to(isolated), "mic imported from source tree"
        evidence["isolated_environment"] = installed
        help_output = run("module_entrypoint", [python, "-m", "mic", "--help"], cwd=work)
        assert "estimate" in help_output and "preflight" in help_output
        definitions = json.loads(
            run("list_structured", [mic, "list", "examples.structured", "--json"])
        )
        assert {row["selector"] for row in definitions} >= {
            "examples.structured:classify",
            "examples.structured:classify_file",
            "examples.structured:tickets",
            "examples.structured:file_tickets",
        }
        snapshots = []
        manifests = []
        for suffix, dataset, evaluation in (
            ("memory", "tickets", "classify"),
            ("file", "file_tickets", "classify_file"),
        ):
            snapshot = json.loads(
                run(
                    f"inspect_{suffix}",
                    [
                        mic,
                        "inspect",
                        f"examples.structured:{dataset}",
                        "--limit",
                        "2",
                    ],
                )
            )
            assert len(snapshot["rows"]) == 2
            snapshots.append(snapshot)
            ready = json.loads(
                run(
                    f"preflight_{suffix}",
                    [
                        mic,
                        "preflight",
                        f"examples.structured:{evaluation}",
                        "--trials",
                        "3",
                    ],
                )
            )
            assert ready["tasks_executed"] == 0 and ready["executions"] == 6
            output = work / f"run-{suffix}"
            manifest = json.loads(
                run(
                    f"run_{suffix}_three_trials",
                    [
                        mic,
                        "run",
                        f"examples.structured:{evaluation}",
                        "--trials",
                        "3",
                        "--output",
                        output,
                        "--json",
                    ],
                )
            )
            assert manifest["counts"]["completed"] == 6 and manifest["counts"]["failed"] == 0
            assert manifest["scores"]["label"]["mean"] == 1.0
            assert manifest["scores"]["evidence"]["mean"] == 1.0
            assert manifest["scores"]["label"]["count"] == 6
            assert (output / "report.html").is_file()
            run(f"rerender_{suffix}", [mic, "report", output, "--output", output / "review.html"])
            assert "structured.classify" in (output / "review.html").read_text(encoding="utf-8")
            manifests.append(manifest)
        assert snapshots[0]["dataset"]["digest"] == snapshots[1]["dataset"]["digest"]
        assert manifests[0]["dataset"]["digest"] == manifests[1]["dataset"]["digest"]
        evidence["structured_smoke"] = {
            "memory_and_file_digests_match": True,
            "digest": manifests[0]["dataset"]["digest"],
            "cases_per_source": 2,
            "trials_per_source": 3,
            "executions_per_source": 6,
            "mean_label": 1.0,
            "mean_evidence": 1.0,
            "inspect_preflight_list_cli_and_rerender": "passed",
        }
        # Recheck at completion so concurrent source edits cannot get a stale PASS.
        evidence.update(verify_archives(wheel, sdist))
        evidence["status"] = "passed"
    except Exception as exc:
        evidence["status"] = "failed"
        evidence["error"] = f"{type(exc).__name__}: {exc}"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(f"packaging: {evidence['status'].upper()}")
    if evidence.get("error"):
        print(evidence["error"], file=sys.stderr)
    print(f"Evidence: {args.output}")
    return int(evidence["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
