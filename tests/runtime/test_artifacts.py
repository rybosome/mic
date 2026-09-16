"""Evaluation artifacts behavior through the real runner."""

import pytest

import mic

from .helpers import evaluation, manifest, row


def test_reporter_failure_keeps_results_and_does_not_repeat_tasks(tmp_path):
    calls = []

    class BrokenReporter:
        name = "broken"

        async def prepare(self):
            pass

        async def report(self, manifest, cases):
            assert (tmp_path / "run.json").exists()
            assert len(cases) == 1
            raise RuntimeError("simulated upload failure")

    result = mic.run(
        evaluation([row()], task=lambda _ctx, value: calls.append(1) or value),
        output=tmp_path,
        reporters=[BrokenReporter()],
    )
    assert calls == [1]
    assert result.exit_code == 1
    assert result.cases[0]["status"] == "completed"
    assert manifest(tmp_path)["reporting"]["broken"]["status"] == "failed"
    assert "simulated upload failure" in (tmp_path / "report.html").read_text()


def test_existing_evidence_is_never_overwritten(tmp_path):
    mic.run(evaluation([row()]), output=tmp_path)
    original = (tmp_path / "run.json").read_bytes()
    with pytest.raises(mic.ConfigurationError, match="must be empty"):
        mic.run(evaluation([row(2)]), output=tmp_path)
    assert (tmp_path / "run.json").read_bytes() == original
