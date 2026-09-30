# Releasing Mic

Mic is early-stage software; API stability is not promised. Releases are deliberate
maintainer actions, not automatic consequences of merging a PR. The distribution is
`mic-evals`; the import and command are `mic`. Versions are manually maintained as
`X.Y.Z` in `pyproject.toml`, with matching `vX.Y.Z` Git tags. The Alpha classifier
describes project maturity; this workflow does not currently accept prerelease
version suffixes.

## Prepare and rehearse

1. Set the version in `pyproject.toml`, run `uv lock`, and commit both files if changed.
   Follow [verification](verification.md), including build and packaging checks.
   Merge the reviewed release preparation to `main`; do not develop on a release tag.
2. In GitHub Actions, select **Release → Run workflow**, choose `main`, and enter the
   proposed tag, e.g. `v0.1.0`. This checks the selected commit against `origin/main`
   and the proposed version; the tag need not exist yet. It runs the full shared CI,
   compatibility matrix, metadata checks, and clean-install checks of both the built
   wheel and a wheel rebuilt from the source distribution.
3. Inspect the successful run and its `package-artifacts` and `verification-evidence`
   artifacts. `release.json` records the commit and upload hashes. Download the
   `release-distributions` artifact if you want to inspect the exact upload files.

Manual runs are verify-only, including when selected on a tag. They never request
publishing identity tokens or enter the `pypi` environment. A rehearsal is not a test
of PyPI authentication. TestPyPI is not required.

## Publish

1. Tag the exact reviewed, merged commit `vX.Y.Z` and push the tag. Do not move
   published release tags. Draft concise GitHub release notes describing the changes,
   early-stage status, limitations, and any breaking changes. GitHub releases are
   the changelog; there is no separately maintained changelog file.
2. Publish the GitHub release for that tag. This is the publication trigger; pushing
   a tag alone does not publish. The workflow requires the tag to match the project
   version and resolve to the triggering commit, which must be an ancestor of `main`.
3. The workflow reruns verification and builds once for this release run. Inspect
   the results and approve the `pypi` environment deployment. This publishes the
   **same verified wheel and source archive**, without rebuilding in the publishing
   job, using Trusted Publishing and attestations. Only this isolated job has
   `id-token: write`; it does not check out or execute repository code.
4. Confirm both files and the description on PyPI. In a fresh environment outside
   the checkout, install `mic-evals==X.Y.Z` from PyPI, run `mic --help`, and exercise
   the README quick start. Record any issues in the release notes.

Publishing is serialized and an active upload is not cancelled by another run.
Pending runs can be superseded by GitHub's concurrency queue; inspect the run status
rather than assuming every queued release executed. Evidence is retained for 14 days.
The account configuration and first real publication must be verified separately;
passing local or rehearsal checks does not prove those external steps work.

## Failed or incorrect releases

- Before any upload: fix the cause. For a transient setup failure, rerun the failed
  jobs of the original run while its verified artifacts remain available. If source
  changes are needed, prepare a new version rather than moving the published tag.
- After any upload: inspect PyPI first. Uploads are not a transaction; one file may
  have succeeded. The workflow intentionally fails on existing files rather than
  silently skipping them. Do not blindly rerun, rebuild, or enable `skip-existing`.
  The simple recovery policy is a new patch version with a complete verified upload.
- For a defective published version: consider yanking it on PyPI with an explanatory
  reason, update the GitHub release notes, and publish a corrected version. Yanking
  is not deletion and does not make the old version unavailable to all installers.
  Do not assume uploaded filenames can be reused or overwritten.

See [PyPI yanking](https://docs.pypi.org/project-management/yanking/) for operational
details. A maintainer makes all publication and recovery decisions.
