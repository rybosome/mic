"""Public sources through suite execution, sinks, artifacts, and offline reports."""

import asyncio
import json
import threading
from dataclasses import replace

import httpx
import pytest

import mic
from examples.yaml_source import YamlDocuments
from mic.providers.bigquery import BigQueryHandle
from mic.providers.braintrust import BraintrustHandle
from mic.reporters import write_report
from mic.sinks.braintrust import BraintrustSink
from tests.providers.test_bigquery_provider import Client, Job, config
from tests.providers.test_braintrust_provider import page
from tests.reporting.test_braintrust import FakeSDK
from tests.runtime.artifact_contract import assert_directory


@pytest.mark.parametrize("provider", ["bigquery", "braintrust", "yaml"])
async def test_streaming_source_to_shared_suite_and_sinks(provider, tmp_path):
    scored = threading.Event()
    async_scored = asyncio.Event()
    requested = []
    client = None
    if provider == "bigquery":

        class PaginatedJob(Job):
            def result(self, **kwargs):
                yield {"input": 1, "expected": 1}
                assert scored.wait(5), "tasks/scorers must run before reading the next page"
                yield {"input": 2, "expected": 2}

        client = Client(job=PaginatedJob())
        handle = BigQueryHandle(
            "fixture",
            "SELECT 1 ORDER BY 1",
            "US",
            client_factory=lambda _: client,
            config_factory=config,
        )
    elif provider == "braintrust":

        async def respond(request):
            requested.append(request)
            if len(requested) == 1:
                return page([{"id": "label", "input": 1, "expected": 1}], "cursor-one")
            if len(requested) == 2:
                await asyncio.wait_for(async_scored.wait(), 5)
                return page([{"id": "label", "input": 2, "expected": 2}], "cursor-two")
            return page([])

        handle = BraintrustHandle(
            "fixture",
            "0123456789abcdef",
            api_key="synthetic-key",
            transport=httpx.MockTransport(respond),
        )
    else:
        path = tmp_path / "data.yaml"
        path.write_text("input: 1\nexpected: 1\n---\ninput: 2\nexpected: 2\n")
        handle = YamlDocuments(path)
    factory_calls = []

    @mic.dataset(input=int, expected=int)
    def source():
        factory_calls.append(True)
        return handle

    @mic.scorer()
    async def exact(ctx):
        scored.set()
        async_scored.set()
        return float(ctx.output == ctx.require_expected())

    @mic.eval(dataset=source, scorers=[exact])
    def baseline(value: int) -> int:
        return value

    candidate = replace(baseline, name="candidate", trials=2)
    sdk = FakeSDK()
    result = await mic.arun(
        [baseline, candidate],
        concurrency=2,
        output=tmp_path / "run",
        sinks=[BraintrustSink(project="fixture", sdk=sdk)],
    )
    assert result.exit_code == 0
    assert factory_calls == [True]
    assert result.summary.trials.completed == 6
    assert len(sdk.experiment.spans) == sdk.experiment.flushes == 6
    assert len(sdk.calls) == 1
    assert all(receipt.status == "completed" for receipt in result.sinks)
    saved, events = assert_directory(tmp_path / "run")
    assert saved == result.to_json()
    accepted = [e for e in events if e["type"] == "case_accepted"]
    assert len(accepted) == 2
    assert len({e["case_id"] for e in accepted}) == 2
    assert write_report(tmp_path / "run").is_file()
    assert "synthetic-key" not in json.dumps(saved)
    if client:
        assert client.closed and len(client.calls) == 2
    if requested:
        assert len(requested) == 3


@pytest.mark.parametrize("policy,completed,code", [("abort", 1, 2), ("skip", 2, 0)])
@pytest.mark.parametrize("provider", ["bigquery", "braintrust", "yaml"])
def test_malformed_records_follow_common_policy_without_swallowing_limits(
    provider, policy, completed, code, tmp_path
):
    if provider == "bigquery":
        client = Client(
            job=Job(rows=[{"input": 1, "expected": 1}, object(), {"input": 2, "expected": 2}])
        )
        handle = BigQueryHandle(
            "fixture", "SELECT 1", "US", client_factory=lambda _: client, config_factory=config
        )
    elif provider == "braintrust":
        responses = iter(
            [
                httpx.Response(
                    200,
                    content='{"id":"one","input":1,"expected":1}\n{broken}\n{"id":"two","input":2,"expected":2}\n',
                    headers={"x-bt-cursor": "next"},
                ),
                page([]),
            ]
        )
        handle = BraintrustHandle(
            "fixture",
            "0123456789abcdef",
            api_key="test",
            transport=httpx.MockTransport(lambda _: next(responses)),
        )
    else:
        path = tmp_path / "data.yaml"
        path.write_text("input: 1\nexpected: 1\n---\nwrong: field\n---\ninput: 2\nexpected: 2\n")
        handle = YamlDocuments(path)

    @mic.dataset(input=int, expected=int)
    def data():
        return handle

    @mic.eval(dataset=data, scorers=[])
    def identity(value: int) -> int:
        return value

    result = mic.run(identity, on_invalid=policy)
    assert result.exit_code == code
    assert result.summary.trials.completed == completed
    assert next(iter(result.sources.values())).records_rejected == 1
