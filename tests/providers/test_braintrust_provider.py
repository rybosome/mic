import asyncio
import json

import httpx
import pytest

from mic.errors import ConfigurationError, DatasetError
from mic.models import MISSING, RawCase, ReadLimits
from mic.providers.braintrust import BraintrustHandle, BraintrustLoader


def page(rows, cursor=None, header="x-bt-cursor"):
    return httpx.Response(
        200,
        content="\n".join(json.dumps(row) for row in rows),
        headers={header: cursor} if cursor is not None else {},
    )


class Fixture:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return next(self.responses)


def handle(**kwargs):
    return BraintrustHandle(dataset_id="existing-dataset", version="0123456789abcdef", **kwargs)


async def collect(fixture, *, source=None, limits=ReadLimits()):
    loader = BraintrustLoader(api_key="test-key", transport=httpx.MockTransport(fixture))
    async with loader.open(source or handle(), limits=limits) as read:
        rows = [row async for row in read.rows()]
        return rows, read.provenance


@pytest.mark.asyncio
async def test_pagination_version_missing_null_source_identity_and_custom_region():
    fixture = Fixture(
        [
            page(
                [
                    {
                        "id": "one",
                        "input": {"nested": [1]},
                        "expected": None,
                        "_xact_id": "source-version",
                    }
                ],
                "cursor-one",
            ),
            page(
                [{"id": "two", "input": "unlabeled", "metadata": {"team": "evals"}}],
                "cursor-two",
                "x-amz-meta-bt_cursor",
            ),
            page([]),
        ]
    )
    rows, provenance = await collect(
        fixture, source=handle(api_url="https://api-eu.braintrust.dev")
    )
    assert all(isinstance(row, RawCase) for row in rows)
    assert rows[0].expected is None
    assert rows[1].expected == MISSING
    assert rows[0].input == {"nested": [1]}
    assert rows[1].id == "two"
    assert rows[0].provenance["record_id"] == "one"
    assert rows[0].provenance["_xact_id"] == "source-version"
    assert provenance["version"] == "0123456789abcdef"
    assert provenance["pages"] == 3
    assert all(
        str(request.url) == "https://api-eu.braintrust.dev/btql" for request in fixture.requests
    )
    assert all(request.method == "POST" for request in fixture.requests)
    bodies = [json.loads(request.content) for request in fixture.requests]
    assert [body["query"]["cursor"] for body in bodies] == [None, "cursor-one", "cursor-two"]
    assert all(body["version"] == "0123456789abcdef" for body in bodies)
    assert all(body["fmt"] == "jsonl" for body in bodies)
    assert bodies[0]["query"]["from"]["args"] == [{"op": "literal", "value": "existing-dataset"}]
    assert all(
        request.headers["Authorization"] == "Bearer test-key" for request in fixture.requests
    )
    assert "test-key" not in repr(provenance)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "responses,match",
    [
        ([page([{"id": "a", "input": 1}])], "missing its pagination cursor"),
        (
            [page([{"id": "a", "input": 1}], "same"), page([{"id": "b", "input": 2}], "same")],
            "repeated pagination cursor",
        ),
        ([page([], "has-cursor")], "empty page returned a cursor"),
        ([page([{"input": 1}], "cursor")], "missing physical record id"),
        ([page([{"id": "a"}], "cursor")], "containing input"),
        ([httpx.Response(200, content="{bad}\n")], "invalid JSON"),
        ([httpx.Response(401, content="secret body")], "HTTP 401"),
        ([httpx.Response(429)], "HTTP 429"),
    ],
)
async def test_protocol_failures(responses, match):
    fixture = Fixture(responses)
    with pytest.raises(DatasetError, match=match) as error:
        await collect(fixture)
    assert "secret body" not in str(error.value)


@pytest.mark.asyncio
async def test_empty_dataset_and_full_cap_uses_terminal_probe():
    rows, provenance = await collect(Fixture([page([])]))
    assert rows == []
    assert provenance["pages"] == 1
    fixture = Fixture([page([{"id": "a", "input": 1}], "a"), page([])])
    rows, _ = await collect(fixture, limits=ReadLimits(max_rows=1))
    assert len(rows) == 1
    assert json.loads(fixture.requests[1].content)["query"]["limit"] == 1


@pytest.mark.asyncio
async def test_row_and_server_page_caps():
    fixture = Fixture([page([{"id": "a", "input": 1}, {"id": "b", "input": 2}], "cursor")])
    with pytest.raises(DatasetError, match="max_rows=1"):
        await collect(fixture, limits=ReadLimits(max_rows=1))
    fixture = Fixture([page([{"id": "a", "input": 1}, {"id": "b", "input": 2}], "cursor")])
    with pytest.raises(DatasetError, match="exceeded requested page limit"):
        await collect(fixture, source=handle(page_size=1))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limits,match",
    [
        (ReadLimits(max_bytes=20), "max_bytes=20"),
        (ReadLimits(max_record_bytes=20), "max_record_bytes=20"),
    ],
)
async def test_byte_caps_before_entire_response_buffering(limits, match):
    class StreamingBody(httpx.AsyncByteStream):
        chunks = 0
        closed = False

        async def __aiter__(self):
            for _ in range(1000):
                self.chunks += 1
                yield b"x" * 1024

        async def aclose(self):
            self.closed = True

    body = StreamingBody()
    fixture = Fixture([httpx.Response(200, stream=body)])
    with pytest.raises(DatasetError, match=match):
        await collect(fixture, limits=limits)
    assert body.chunks < 1000
    assert body.closed


@pytest.mark.asyncio
async def test_injected_client_stays_open_and_owned_transport_closes():
    class Transport(httpx.MockTransport):
        closed = False

        async def aclose(self):
            self.closed = True

    transport = Transport(lambda _: page([]))
    async with httpx.AsyncClient(transport=transport) as client:
        loader = BraintrustLoader(client=client, api_key="test-key")
        async with loader.open(handle(), limits=ReadLimits()) as read:
            assert [row async for row in read.rows()] == []
        assert not client.is_closed
        assert not transport.closed
    assert transport.closed
    transport = Transport(lambda _: httpx.Response(500))
    with pytest.raises(DatasetError):
        async with BraintrustLoader(api_key="test", transport=transport).open(
            handle(), limits=ReadLimits()
        ) as read:
            _ = [row async for row in read.rows()]
    assert transport.closed


@pytest.mark.asyncio
async def test_timeout_and_cancel_close_response():
    class SlowBody(httpx.AsyncByteStream):
        closed = False
        started = asyncio.Event()

        async def __aiter__(self):
            self.started.set()
            await asyncio.sleep(10)
            yield b""

        async def aclose(self):
            self.closed = True

    body = SlowBody()
    task = asyncio.create_task(collect(Fixture([httpx.Response(200, stream=body)])))
    await body.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert body.closed

    def timeout(_):
        raise httpx.ReadTimeout("fixture deadline")

    with pytest.raises(DatasetError, match="fixture deadline"):
        await collect(timeout)


@pytest.mark.asyncio
async def test_no_credentials_or_version_fails_before_http(monkeypatch):
    monkeypatch.delenv("BRAINTRUST_API_KEY", raising=False)

    def forbidden(_):
        pytest.fail("HTTP request before configuration validation")

    loader = BraintrustLoader(transport=httpx.MockTransport(forbidden))
    with pytest.raises(ConfigurationError, match="API_KEY"):
        async with loader.open(handle(), limits=ReadLimits()):
            pass
    for version in ("", "latest", "head"):
        with pytest.raises(ConfigurationError, match="pinned version"):
            await collect(forbidden, source=BraintrustHandle("id", version))
    with pytest.raises(ConfigurationError, match="HTTPS"):
        await collect(forbidden, source=handle(api_url="http://remote.example"))


@pytest.mark.asyncio
async def test_malformed_key_is_rejected_without_exposing_it_in_exception():
    key = "credential-value\naccidental-newline"
    loader = BraintrustLoader(api_key=key)
    with pytest.raises(ConfigurationError, match="one non-whitespace ASCII token") as error:
        async with loader.open(handle(), limits=ReadLimits()):
            pass
    assert "credential-value" not in str(error.value)
