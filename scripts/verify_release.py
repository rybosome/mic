"""Fail closed on release identity and distribution-set mismatches; never publish."""

import argparse
import hashlib
import json
import re
import subprocess
import tarfile
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def project_identity(root: Path) -> tuple[str, str]:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    name, version = project["name"], project["version"]
    # Intentionally small release policy, not a second PEP 440 implementation.
    if name != "mic-evals" or not re.fullmatch(
        r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", version
    ):
        raise ValueError("Expected mic-evals with an X.Y.Z version (alpha project status is OK)")
    return name, version


def verify_source(root: Path, tag: str, *, require_tag: bool) -> str:
    _, version = project_identity(root)
    if tag != f"v{version}":
        raise ValueError(f"Tag must match project version: v{version}")
    revision = git(root, "rev-parse", "HEAD")
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", revision, "refs/remotes/origin/main"],
        cwd=root,
        check=True,
    )
    if (
        require_tag
        and git(root, "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}") != revision
    ):
        raise ValueError("Release tag does not resolve to the checked-out commit")
    return revision


def check_metadata(text: str, name: str, version: str) -> None:
    metadata = Parser().parsestr(text)
    if metadata.get_all("Name") != [name] or metadata.get_all("Version") != [version]:
        raise ValueError("Distribution name/version differs from pyproject.toml")


def verify_distributions(root: Path, directory: Path) -> dict:
    name, version = project_identity(root)
    stem = f"mic_evals-{version}"
    expected = {f"{stem}-py3-none-any.whl", f"{stem}.tar.gz"}
    paths = list(directory.iterdir())
    if {path.name for path in paths} != expected or any(
        not path.is_file() or path.is_symlink() for path in paths
    ):
        raise ValueError("Upload directory must contain exactly the expected wheel and sdist")
    with zipfile.ZipFile(directory / f"{stem}-py3-none-any.whl") as archive:
        metadata_path = f"{stem}.dist-info/METADATA"
        metadata_members = [name for name in archive.namelist() if name.endswith("/METADATA")]
        if metadata_members != [metadata_path]:
            raise ValueError("Wheel must contain exactly its expected METADATA")
        check_metadata(archive.read(metadata_path).decode("utf-8"), name, version)
    with tarfile.open(directory / f"{stem}.tar.gz") as archive:
        for member, kind in (
            (f"{stem}/PKG-INFO", "metadata"),
            (f"{stem}/pyproject.toml", "project"),
        ):
            matches = [entry for entry in archive.getmembers() if entry.name == member]
            if len(matches) != 1 or not matches[0].isfile():
                raise ValueError(f"Source distribution must contain one regular {member}")
            stream = archive.extractfile(matches[0])
            if stream is None:
                raise ValueError(f"Cannot read {member}")
            text = stream.read().decode("utf-8")
            if kind == "metadata":
                check_metadata(text, name, version)
            else:
                project = tomllib.loads(text)["project"]
                if (project["name"], project["version"]) != (name, version):
                    raise ValueError("Source distribution project identity differs from checkout")
    return {
        "commit": git(root, "rev-parse", "HEAD"),
        "name": name,
        "version": version,
        "distributions": {
            path.name: {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in sorted(paths)
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    source = commands.add_parser("source", help="Validate a main-based release candidate")
    source.add_argument("--tag", required=True)
    source.add_argument("--require-tag", action="store_true")
    artifacts = commands.add_parser("artifacts", help="Validate the exact upload set")
    artifacts.add_argument("--dist", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    if args.command == "source":
        print(verify_source(ROOT, args.tag, require_tag=args.require_tag))
    else:
        evidence = verify_distributions(ROOT, args.dist)
        output = ROOT / ".artifacts/packaging/release.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        print(f"Release distributions verified: {output}")


if __name__ == "__main__":
    main()
