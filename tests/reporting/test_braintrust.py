"""Export projection, SDK compatibility, and failure propagation."""

import copy
from types import SimpleNamespace

import pytest

from mic._runtime.aggregation import Aggregator
from mic.errors import ConfigurationError
from mic.results import EvaluationOutcome, RunInfo
from mic.sinks import TrialFinished
from mic.sinks.braintrust import BraintrustSink

from ._fixtures import sample


class FakeSpan:
    def __init__(self) -> None:
        self.event: dict[str, object] = {}
        self.ended = False

    def log(self, **event: object) -> None:
        self.event = event

    def end(self) -> None:
        self.ended = True


class FakeExperiment:
    id = "exp-123"

    def __init__(self, *, fail_flush: bool = False) -> None:
        self.spans: list[FakeSpan] = []
        self.flushes = 0
        self.fail_flush = fail_flush

    def start_span(self, **kwargs: object) -> FakeSpan:
        assert kwargs["set_current"] is False
        span = FakeSpan()
        self.spans.append(span)
        return span

    def flush(self) -> None:
        self.flushes += 1
        if self.fail_flush:
            raise ConnectionError("Demonstration flush failure")

    def summarize(self, *, summarize_scores: bool) -> SimpleNamespace:
        assert summarize_scores is False
        return SimpleNamespace(experiment_url="https://www.braintrust.dev/experiment/demo")


class FakeSDK:
    def __init__(self, *, fail_flush: bool = False) -> None:
        self.calls: list[dict[str, object]] = []
        self.experiment = FakeExperiment(fail_flush=fail_flush)

    def init(self, **kwargs: object) -> FakeExperiment:
        self.calls.append(kwargs)
        return self.experiment


async def prepared_sink(sdk: object, *, project: str = "test") -> BraintrustSink:
    sink = BraintrustSink(project=project, sdk=sdk)
    await sink.prepare()
    return sink


async def export(sink, manifest, cases):
    info = RunInfo(manifest["run_id"], "2026-10-02T00:00:00+00:00", {})
    async with sink.open(info) as session:
        for case in cases:
            await session.write(
                TrialFinished(
                    "test", "source", case["row_index"], case["trial"], case["case_id"], case
                )
            )
        return await session.finish(
            EvaluationOutcome(info.run_id, "completed", 0, Aggregator({}).snapshot(), {}, ())
        )


async def test_braintrust_prepare_is_read_only_and_report_preserves_evidence() -> None:
    manifest, cases = sample()
    before = copy.deepcopy(cases)
    sdk = FakeSDK()
    sink = BraintrustSink(project="test", sdk=sdk)
    await sink.prepare()
    assert sdk.calls == []
    result = await export(sink, manifest, cases)
    assert result.status == "completed"
    assert result.details["rows"] == 2
    assert sdk.calls[0]["set_current"] is False
    assert sdk.calls[0]["update"] is False
    events = [span.event for span in sdk.experiment.spans]
    assert "expected" not in events[0]
    assert events[1]["expected"] is None
    assert events[0]["input"] is None and events[0]["output"] is None
    assert events[0]["scores"] == {"exact": None}
    assert events[1]["metadata"] == {
        "mic": {
            "run_id": "run-1",
            "dataset": {"source_id": "source", "task": "test"},
            "case": cases[1],
        }
    }
    assert all(span.ended for span in sdk.experiment.spans)
    assert sdk.experiment.flushes == 2
    assert cases == before


@pytest.mark.parametrize("value", [-1.0, 1.1, True, float("inf"), float("nan")])
async def test_braintrust_invalid_score_fails_before_experiment_creation(value: object) -> None:
    manifest, cases = sample()
    cases[0]["scores"] = [{"name": "cost", "value": value}]  # type: ignore[assignment]
    sdk = FakeSDK()
    sink = await prepared_sink(sdk)
    with pytest.raises(ConfigurationError, match="only accepts scores"):
        await export(sink, manifest, cases)
    assert sdk.calls == []


async def test_flush_failure_is_not_success() -> None:
    manifest, cases = sample()
    sdk = FakeSDK(fail_flush=True)
    sink = await prepared_sink(sdk)
    with pytest.raises(ConnectionError, match="flush failure"):
        await export(sink, manifest, cases)
    assert len(sdk.calls) == 1
    assert len(sdk.experiment.spans) == 1


async def test_sink_requires_credentials_without_initializing_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BRAINTRUST_API_KEY", raising=False)
    with pytest.raises(ConfigurationError, match="BRAINTRUST_API_KEY"):
        await BraintrustSink(project="test").prepare()


def test_installed_braintrust_sdk_preserves_projection_in_namespaced_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Optional local SDK compatibility check; deliberately no server/auth verification."""
    import socket

    logger = pytest.importorskip(
        "braintrust.logger", reason="optional Braintrust SDK not installed"
    )
    from braintrust.util import LazyValue

    from mic.sinks.braintrust import _event

    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("SDK projection contract must not access the network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    metadata = logger.ProjectExperimentMetadata(
        project=logger.ObjectMetadata("project", "test", {}),
        experiment=logger.ObjectMetadata("experiment", "test", {}),
    )
    experiment = logger.Experiment(lazy_metadata=LazyValue(lambda: metadata, use_mutex=False))
    manifest, cases = sample()
    with logger._internal_with_memory_background_logger() as memory:
        for index, case in enumerate(cases):
            event = _event(
                manifest["run_id"],
                TrialFinished(
                    "test", "source", case["row_index"], case["trial"], case["case_id"], case
                ),
            )
            span = experiment.start_span(name="case", id=str(index), set_current=False)
            span.log(**event)
            span.end()
        experiment.flush()
        rows = memory.pop()
    assert len(rows) == 2
    assert rows[0]["scores"] == {"exact": None}
    assert "expected" not in rows[0]["metadata"]["mic"]["case"]
    assert rows[1]["metadata"]["mic"]["case"]["expected"] is None
    assert rows[0]["metadata"]["mic"]["case"]["input"] is None
    assert rows[0]["metadata"]["mic"]["case"]["output"] is None
    # SDK 0.39.0 strips top-level nulls; the exact original case is still retained above.
    assert "expected" not in rows[1]


def test_installed_sdk_synchronous_export_state_is_owned_and_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    sdk = pytest.importorskip("braintrust", reason="optional Braintrust SDK not installed")
    from mic.sinks.braintrust import _synchronous_state

    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("Preparing owned SDK state must not access the network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    state = _synchronous_state(sdk)
    assert state.global_bg_logger().sync_flush is True
    assert state.global_bg_logger().started is False
    assert state is not sdk.logger._state


async def test_sink_flushes_each_trial_without_batch_retention() -> None:
    manifest, cases = sample()
    sdk = FakeSDK()
    sink = await prepared_sink(sdk)
    await export(sink, manifest, cases * 101)
    assert len(sdk.experiment.spans) == 202
    assert sdk.experiment.flushes == 202


def test_installed_sdk_sync_mode_raises_failed_upload_without_network() -> None:
    sdk = pytest.importorskip("braintrust", reason="optional Braintrust SDK not installed")
    from braintrust.util import LazyValue

    from mic.sinks.braintrust import _synchronous_state

    state = _synchronous_state(sdk)
    logger = state.global_bg_logger()
    logger.num_tries = 1
    fake_connection = SimpleNamespace(
        post=lambda *args, **kwargs: SimpleNamespace(
            ok=False, status_code=503, text="Demonstration rejected upload"
        )
    )
    logger.api_conn = LazyValue(lambda: fake_connection, use_mutex=False)
    item = sdk.logger.stringify_with_overflow_meta({"id": "row", "experiment_id": "experiment"})
    with pytest.raises(Exception, match="Demonstration rejected upload"):
        logger._submit_logs_request(
            [item], {"max_request_size": 1_000_000, "can_use_overflow": False}
        )


async def test_sink_cancellation_drains_upload_thread() -> None:
    import asyncio
    import threading

    started = threading.Event()
    release = threading.Event()

    class BlockingSDK(FakeSDK):
        def init(self, **kwargs: object) -> FakeExperiment:
            started.set()
            assert release.wait(5), "test failed to release SDK thread"
            return super().init(**kwargs)

    manifest, cases = sample()
    sdk = BlockingSDK()
    sink = await prepared_sink(sdk)
    task = asyncio.create_task(export(sink, manifest, cases))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "cancelled report must wait for its active SDK thread"
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "repeated cancellation must still join the SDK thread"
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(sdk.calls) == 1
    assert len(sdk.experiment.spans) == 1
    assert sdk.experiment.flushes == 1


@pytest.mark.parametrize(
    "scores",
    [None, {}, [None], [{"name": 1}], [{"name": "exact"}, {"name": "exact"}]],
)
async def test_malformed_projection_fails_before_any_remote_write(scores: object) -> None:
    manifest, cases = sample()
    cases[0]["scores"] = scores
    sdk = FakeSDK()
    sink = await prepared_sink(sdk)
    with pytest.raises(ConfigurationError):
        await export(sink, manifest, cases)
    assert sdk.calls == []


async def test_sdk_payload_mutation_cannot_change_local_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, cases = sample()
    original = copy.deepcopy((manifest, cases))

    def mutate(self: FakeSpan, **event: object) -> None:
        event["metadata"]["mic"]["case"]["provenance"]["line"] = 999
        event["metadata"]["mic"]["dataset"]["name"] = "mutated"
        event["scores"]["exact"] = 1

    monkeypatch.setattr(FakeSpan, "log", mutate)
    sink = await prepared_sink(FakeSDK())
    await export(sink, manifest, cases)
    assert (manifest, cases) == original


async def test_log_failure_ends_span_without_retrying_uncertain_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, cases = sample()
    sdk = FakeSDK()

    def fail_log(self: FakeSpan, **event: object) -> None:
        raise ValueError("original log error")

    def fail_end(self: FakeSpan) -> None:
        self.ended = True

    monkeypatch.setattr(FakeSpan, "log", fail_log)
    monkeypatch.setattr(FakeSpan, "end", fail_end)
    sink = await prepared_sink(sdk)
    with pytest.raises(ValueError, match="original log error"):
        await export(sink, manifest, cases)

    assert sdk.experiment.spans[0].ended
    assert sdk.experiment.flushes == 0
    assert len(sdk.experiment.spans) == 1


async def test_running_evaluation_persists_repeated_export_cancellation_after_thread_finishes(
    tmp_path,
) -> None:
    import asyncio
    import json
    import threading

    import mic

    started = threading.Event()
    release = threading.Event()
    calls = []

    @mic.dataset(name="upload.data", schema=mic.case_schema(input=str, expected=str))
    def data():
        return [{"id": "one", "input": "value", "expected": "value"}]

    @mic.eval(name="upload.eval", dataset=data, output=str, scorers=[])
    def evaluation(context, value):
        calls.append(value)
        return value

    class BlockingSDK(FakeSDK):
        def init(self, **kwargs: object) -> FakeExperiment:
            started.set()
            assert release.wait(5), "test failed to release SDK thread"
            return super().init(**kwargs)

    sdk = BlockingSDK()
    pending = asyncio.create_task(
        mic.arun(
            evaluation,
            output=tmp_path / "run",
            sinks=[BraintrustSink(project="test", sdk=sdk)],
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 2)
        for _ in range(3):
            pending.cancel()
            await asyncio.sleep(0)
            assert not pending.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    manifest = json.loads((tmp_path / "run/run.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "cancelled" and manifest["exit_code"] == 130
    assert next(s for s in manifest["sinks"] if s["name"] == "braintrust")["status"] == "cancelled"
    assert manifest["summary"]["trials"]["completed"] == 1
    assert sdk.experiment.flushes == 1
    assert (tmp_path / "run/events.jsonl").is_file()
    assert calls == ["value"], "export cancellation must not replay the task"


async def test_log_failure_is_primary_when_span_cleanup_also_fails(monkeypatch):
    def broken_log(self, **event):
        raise ValueError("primary")

    def broken_end(self):
        self.ended = True
        raise RuntimeError("private cleanup detail")

    monkeypatch.setattr(FakeSpan, "log", broken_log)
    monkeypatch.setattr(FakeSpan, "end", broken_end)
    sdk = FakeSDK()
    with pytest.raises(ValueError, match="primary") as error:
        await export(await prepared_sink(sdk), *sample())
    assert sdk.experiment.spans[0].ended
    assert sdk.experiment.flushes == 0
    assert error.value.__notes__ == ["Span cleanup also failed (RuntimeError)"]


@pytest.mark.parametrize(
    "options",
    [
        {"project": ""},
        {"project": "test", "experiment": ""},
        {"project": "test", "app_url": "https://user:password@host"},
        {"project": "test", "app_url": "http://example.com"},
    ],
)
async def test_bad_destination_fails_before_sdk_init(options):
    sdk = FakeSDK()
    with pytest.raises(ConfigurationError):
        await BraintrustSink(**options, sdk=sdk).prepare()
    assert sdk.calls == []


async def test_later_invalid_score_leaves_known_partial_export():
    manifest, cases = sample()
    cases[1]["scores"] = [{"name": "cost", "value": 2}]
    sdk = FakeSDK()
    with pytest.raises(ConfigurationError):
        await export(await prepared_sink(sdk), manifest, cases)
    assert len(sdk.calls) == len(sdk.experiment.spans) == sdk.experiment.flushes == 1


async def test_no_trial_events_do_not_create_remote_experiment():
    sdk = FakeSDK()
    receipt = await export(await prepared_sink(sdk), sample()[0], [])
    assert sdk.calls == []
    assert receipt.details == {"rows": 0}
