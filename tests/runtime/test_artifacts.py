"""Explicit evidence and sink failure behavior through the real runner."""

import pytest

import mic

from .helpers import cases, evaluation, manifest, row
from .test_streaming import Capture


def test_sink_failure_keeps_results_and_does_not_repeat_tasks(tmp_path):
    calls = []

    class Broken(Capture):
        async def finish(self, outcome):
            raise RuntimeError("private response")

    result = mic.run(
        evaluation([row()], task=lambda value: calls.append(1) or value),
        output=tmp_path,
        sinks=[Broken()],
    )
    assert calls == [1] and result.exit_code == 1
    assert cases(result)[0]["status"] == "completed"
    assert manifest(tmp_path)["sinks"][-1]["status"] == "failed"
    assert not (tmp_path / "report.html").exists()


def test_existing_evidence_is_never_overwritten(tmp_path):
    mic.run(evaluation([row()]), output=tmp_path)
    original = (tmp_path / "run.json").read_bytes()
    with pytest.raises(mic.ConfigurationError, match="must be empty"):
        mic.run(evaluation([row(2)]), output=tmp_path)
    assert (tmp_path / "run.json").read_bytes() == original
