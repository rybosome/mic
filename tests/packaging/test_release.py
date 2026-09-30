"""Release gates reject wrong revisions and unintended upload contents offline."""

import hashlib
import io
import subprocess
import tarfile
import zipfile

import pytest

from scripts.verify_release import (
    check_metadata,
    git,
    project_identity,
    verify_distributions,
    verify_source,
)


@pytest.fixture
def repository(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "mic-evals"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    git(tmp_path, "init")
    git(tmp_path, "config", "user.name", "Release Test")
    git(tmp_path, "config", "user.email", "release@example.invalid")
    git(tmp_path, "add", "pyproject.toml")
    git(tmp_path, "-c", "commit.gpgsign=false", "commit", "-m", "Initial candidate")
    git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    return tmp_path


def make_distributions(
    root, *, wheel_version="0.1.0", sdist_version="0.1.0", source_version="0.1.0"
):
    directory = root / "dist"
    directory.mkdir(exist_ok=True)
    stem = "mic_evals-0.1.0"
    with zipfile.ZipFile(directory / f"{stem}-py3-none-any.whl", "w") as archive:
        archive.writestr(
            f"{stem}.dist-info/METADATA", f"Name: mic-evals\nVersion: {wheel_version}\n"
        )
    with tarfile.open(directory / f"{stem}.tar.gz", "w:gz") as archive:
        for filename, text in (
            ("PKG-INFO", f"Name: mic-evals\nVersion: {sdist_version}\n"),
            ("pyproject.toml", f'[project]\nname = "mic-evals"\nversion = "{source_version}"\n'),
        ):
            data = text.encode()
            entry = tarfile.TarInfo(f"{stem}/{filename}")
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
    return directory


def test_rehearsal_checks_main_candidate_without_creating_tag(repository):
    revision = verify_source(repository, "v0.1.0", require_tag=False)
    assert revision == git(repository, "rev-parse", "HEAD")
    assert git(repository, "tag", "--list") == ""


@pytest.mark.parametrize("annotated", [False, True])
def test_release_accepts_matching_lightweight_or_annotated_tag(repository, annotated):
    arguments = ["-a", "-m", "Release"] if annotated else []
    git(repository, "-c", "tag.gpgsign=false", "tag", *arguments, "v0.1.0")
    assert verify_source(repository, "v0.1.0", require_tag=True)


@pytest.mark.parametrize("tag", ["v0.2.0", "0.1.0", "main", "--help", "v0.1.0\nextra"])
def test_rejects_wrong_version_or_invalid_tag(repository, tag):
    with pytest.raises(ValueError, match="Tag must match"):
        verify_source(repository, tag, require_tag=False)


def test_publication_requires_existing_tag(repository):
    with pytest.raises(subprocess.CalledProcessError):
        verify_source(repository, "v0.1.0", require_tag=True)


def test_rejects_tag_pointing_to_different_commit(repository):
    git(repository, "-c", "tag.gpgsign=false", "tag", "v0.1.0")
    git(repository, "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "Later")
    git(repository, "update-ref", "refs/remotes/origin/main", "HEAD")
    with pytest.raises(ValueError, match="does not resolve"):
        verify_source(repository, "v0.1.0", require_tag=True)


def test_rejects_unmerged_candidate(repository):
    git(repository, "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "Not merged")
    with pytest.raises(subprocess.CalledProcessError):
        verify_source(repository, "v0.1.0", require_tag=False)


def test_rejects_missing_main_reference(repository):
    git(repository, "update-ref", "-d", "refs/remotes/origin/main")
    with pytest.raises(subprocess.CalledProcessError):
        verify_source(repository, "v0.1.0", require_tag=False)


def test_distribution_evidence_binds_commit_and_exact_bytes(repository):
    directory = make_distributions(repository)
    evidence = verify_distributions(repository, directory)
    assert evidence["commit"] == git(repository, "rev-parse", "HEAD")
    assert evidence["name"] == "mic-evals"
    assert evidence["version"] == "0.1.0"
    assert evidence["distributions"] == {
        path.name: {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for path in directory.iterdir()
    }


@pytest.mark.parametrize("field", ["wheel_version", "sdist_version", "source_version"])
def test_rejects_metadata_version_mismatch(repository, field):
    directory = make_distributions(repository, **{field: "0.2.0"})
    with pytest.raises(ValueError, match="differs"):
        verify_distributions(repository, directory)


@pytest.mark.parametrize(
    "metadata",
    [
        "Name: another-project\nVersion: 0.1.0\n",
        "Name: mic-evals\nVersion: 0.1.0\nVersion: 0.2.0\n",
        "Name: mic-evals\n",
    ],
)
def test_rejects_wrong_missing_or_ambiguous_metadata(metadata):
    with pytest.raises(ValueError, match="differs"):
        check_metadata(metadata, "mic-evals", "0.1.0")


@pytest.mark.parametrize("metadata_path", ["wrong.dist-info/METADATA", "no-metadata"])
def test_rejects_missing_or_misplaced_wheel_metadata(repository, metadata_path):
    directory = make_distributions(repository)
    with zipfile.ZipFile(directory / "mic_evals-0.1.0-py3-none-any.whl", "w") as archive:
        archive.writestr(metadata_path, "Name: mic-evals\nVersion: 0.1.0\n")
    with pytest.raises(ValueError, match="expected METADATA"):
        verify_distributions(repository, directory)


def test_rejects_sdist_without_project_metadata(repository):
    directory = make_distributions(repository)
    with tarfile.open(directory / "mic_evals-0.1.0.tar.gz", "w:gz"):
        pass
    with pytest.raises(ValueError, match="one regular"):
        verify_distributions(repository, directory)


@pytest.mark.parametrize("change", ["extra", "missing", "directory", "stale"])
def test_rejects_unintended_upload_set(repository, change):
    directory = make_distributions(repository)
    wheel = directory / "mic_evals-0.1.0-py3-none-any.whl"
    if change == "extra":
        (directory / "private.txt").write_text("synthetic fixture", encoding="utf-8")
    elif change == "missing":
        wheel.unlink()
    elif change == "directory":
        wheel.unlink()
        wheel.mkdir()
    else:
        wheel.rename(directory / "mic_evals-0.0.1-py3-none-any.whl")
    with pytest.raises(ValueError, match="exactly the expected"):
        verify_distributions(repository, directory)


@pytest.mark.parametrize("version", ["01.0.0", "0.1.0rc1", "0.1", "0.1.0/post"])
def test_release_version_policy_is_explicit(repository, version):
    (repository / "pyproject.toml").write_text(
        f'[project]\nname = "mic-evals"\nversion = "{version}"\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="X.Y.Z"):
        project_identity(repository)
