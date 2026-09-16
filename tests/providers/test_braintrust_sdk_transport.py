"""Default source transport: real pinned SDK, fake HTTP, no HTTPX requirement."""

import asyncio
import importlib
import json
import threading

import pytest

from mic.errors import ConfigurationError, DatasetError
from mic.models import ReadLimits
from mic.providers.braintrust import BraintrustHandle, BraintrustLoader


@pytest.fixture
def requests_sdk(monkeypatch):
    pytest.importorskip("braintrust.api", reason="optional Braintrust SDK not installed")
    requests = importlib.import_module("requests")
    imported = importlib.import_module

    def guarded_import(name, package=None):
        if name == "httpx" or name.startswith("httpx."):
            pytest.fail("default Braintrust source tried to import HTTPX")
        return imported(name, package)

    monkeypatch.setattr(importlib, "import_module", guarded_import)
    monkeypatch.setattr(
        "requests.sessions.get_netrc_auth", lambda _: pytest.fail("unexpected netrc read")
    )
    return requests


class RawBody:
    def __init__(self, content):
        self.content = content
        self.closed = False
        self.released = False
        self.chunks = 0

    def stream(self, chunk_size, decode_content=True):
        for start in range(0, len(self.content), chunk_size):
            self.chunks += 1
            yield self.content[start : start + chunk_size]

    def close(self):
        self.closed = True

    def release_conn(self):
        self.released = True


def response(requests_sdk, content=b"", cursor=None, status=200):
    class StreamingResponse(requests_sdk.Response):
        @property
        def content(self):
            raise AssertionError("response.content would buffer an unbounded body")

    value = StreamingResponse()
    value.status_code = status
    value.raw = RawBody(content)
    if cursor is not None:
        value.headers["x-bt-cursor"] = cursor
    return value


def capture_session(monkeypatch, requests_sdk, responses):
    calls = []
    closed = []
    owned = set()
    iterator = iter(responses)
    initialize_session = requests_sdk.Session.__init__
    close_session = requests_sdk.Session.close

    def initialize(session):
        initialize_session(session)
        owned.add(session)

    def send(session, request, **kwargs):
        calls.append((session, request, kwargs))
        assert kwargs["stream"] is True
        assert kwargs["allow_redirects"] is False
        return next(iterator)

    def close(session):
        if session in owned:
            closed.append(session)
        close_session(session)

    monkeypatch.setattr(requests_sdk.Session, "__init__", initialize)
    monkeypatch.setattr(requests_sdk.Session, "send", send)
    monkeypatch.setattr(requests_sdk.Session, "close", close)
    return calls, closed


@pytest.mark.asyncio
async def test_default_sdk_reads_pinned_pages_and_closes_owned_session(requests_sdk, monkeypatch):
    first = response(requests_sdk, b'{"id":"a","input":"x","expected":null}\n', "page-one")
    terminal = response(requests_sdk)
    calls, closed = capture_session(monkeypatch, requests_sdk, [first, terminal])
    source = BraintrustHandle(
        "fixture-dataset",
        "pinned-version",
        api_url="https://api-eu.braintrust.dev",
        page_size=1,
        timeout=0.5,
    )
    async with BraintrustLoader(api_key="fixture-key").open(source, limits=ReadLimits()) as read:
        rows = [row async for row in read.rows()]
    assert len(rows) == 1 and rows[0].expected is None
    assert all(value.raw.closed or value.raw.released for value in (first, terminal))
    assert len(closed) == 1 and closed[0] is calls[0][0]
    assert all(call[2]["timeout"] == 0.5 for call in calls)
    assert all(call[1].headers["Authorization"] == "Bearer fixture-key" for call in calls)
    assert all(call[1].url == "https://api-eu.braintrust.dev/btql" for call in calls)
    bodies = [json.loads(call[1].body) for call in calls]
    assert [body["query"]["cursor"] for body in bodies] == [None, "page-one"]
    assert all(body["fmt"] == "jsonl" and body["version"] == "pinned-version" for body in bodies)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,match", [(401, "HTTP 401"), (302, "HTTP 302"), (500, "HTTP 500")])
async def test_sdk_http_errors_close_without_buffering_bodies(
    requests_sdk, monkeypatch, status, match
):
    value = response(requests_sdk, b"x" * 1_000_000, status=status)
    _, closed = capture_session(monkeypatch, requests_sdk, [value])
    with pytest.raises(DatasetError, match=match):
        async with BraintrustLoader(api_key="fixture").open(
            BraintrustHandle("dataset", "pinned"), limits=ReadLimits()
        ) as read:
            _ = [row async for row in read.rows()]
    assert value.raw.closed and value.raw.chunks == 0
    assert len(closed) == 1


@pytest.mark.asyncio
async def test_sdk_response_byte_cap_stops_streaming_and_closes(requests_sdk, monkeypatch):
    value = response(requests_sdk, b"x" * 1_000_000)
    _, closed = capture_session(monkeypatch, requests_sdk, [value])
    with pytest.raises(DatasetError, match="max_bytes=20"):
        async with BraintrustLoader(api_key="fixture").open(
            BraintrustHandle("dataset", "pinned"), limits=ReadLimits(max_bytes=20)
        ) as read:
            _ = [row async for row in read.rows()]
    assert value.raw.chunks == 1
    assert value.raw.closed and len(closed) == 1


@pytest.mark.asyncio
async def test_sdk_cancelled_response_creation_joins_and_closes(requests_sdk, monkeypatch):
    value = response(requests_sdk)
    started = threading.Event()
    released = threading.Event()
    calls, closed = capture_session(monkeypatch, requests_sdk, [value])
    original_send = requests_sdk.Session.send

    def send(session, request, **kwargs):
        started.set()
        assert released.wait(2)
        return original_send(session, request, **kwargs)

    monkeypatch.setattr(requests_sdk.Session, "send", send)

    async def consume():
        async with BraintrustLoader(api_key="fixture").open(
            BraintrustHandle("dataset", "pinned"), limits=ReadLimits()
        ) as read:
            return [row async for row in read.rows()]

    task = asyncio.create_task(consume())
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    released.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) == len(closed) == 1
    assert value.raw.closed


@pytest.mark.asyncio
async def test_repeated_cancel_waits_for_late_response_cleanup_before_client_close(
    requests_sdk,
    monkeypatch,
):
    request_started = threading.Event()
    request_released = threading.Event()
    close_started = threading.Event()
    close_released = threading.Event()

    class ClosingBody(RawBody):
        def close(self):
            close_started.set()
            assert close_released.wait(3), "test must release response close"
            super().close()

    value = response(requests_sdk)
    value.raw = ClosingBody(b"")
    _, closed = capture_session(monkeypatch, requests_sdk, [value])
    original_send = requests_sdk.Session.send

    def send(session, request, **kwargs):
        request_started.set()
        assert request_released.wait(3), "test must release HTTP request"
        return original_send(session, request, **kwargs)

    monkeypatch.setattr(requests_sdk.Session, "send", send)

    async def consume():
        async with BraintrustLoader(api_key="fixture").open(
            BraintrustHandle("dataset", "pinned"), limits=ReadLimits()
        ) as read:
            return [row async for row in read.rows()]

    task = asyncio.create_task(consume())
    try:
        assert await asyncio.to_thread(request_started.wait, 2)
        task.cancel()
        request_released.set()
        assert await asyncio.to_thread(close_started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not closed
    finally:
        request_released.set()
        close_released.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert value.raw.closed and len(closed) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_sdk_cancelled_stream_read_finishes_before_resources_close(
    requests_sdk,
    monkeypatch,
    cancel_count,
):
    started = threading.Event()
    released = threading.Event()
    finished = threading.Event()
    prematurely_closed = threading.Event()

    class SlowBody(RawBody):
        def stream(self, chunk_size, decode_content=True):
            started.set()
            assert released.wait(2)
            assert not self.closed
            finished.set()
            yield b'{"id":"a","input":"x"}\n'

        def close(self):
            if not finished.is_set():
                prematurely_closed.set()
            super().close()

    value = response(requests_sdk, cursor="next")
    value.raw = SlowBody(b"")
    _, closed = capture_session(monkeypatch, requests_sdk, [value])

    async def consume():
        async with BraintrustLoader(api_key="fixture").open(
            BraintrustHandle("dataset", "pinned"), limits=ReadLimits()
        ) as read:
            return [row async for row in read.rows()]

    task = asyncio.create_task(consume())
    try:
        assert await asyncio.to_thread(started.wait, 2)
        for _ in range(cancel_count):
            task.cancel()
            await asyncio.sleep(0)
        assert not await asyncio.to_thread(prematurely_closed.wait, 0.05)
    finally:
        released.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert finished.is_set()
    assert value.raw.closed and len(closed) == 1


@pytest.mark.asyncio
async def test_sdk_timeout_closes_client(requests_sdk, monkeypatch):
    _, closed = capture_session(monkeypatch, requests_sdk, [])

    def timeout(session, request, **kwargs):
        raise requests_sdk.exceptions.ReadTimeout("fixture read deadline")

    monkeypatch.setattr(requests_sdk.Session, "send", timeout)
    with pytest.raises(DatasetError, match="fixture read deadline"):
        async with BraintrustLoader(api_key="fixture").open(
            BraintrustHandle("dataset", "pinned"), limits=ReadLimits()
        ) as read:
            _ = [row async for row in read.rows()]
    assert len(closed) == 1


@pytest.mark.asyncio
async def test_missing_sdk_fails_with_named_extra(monkeypatch):
    imported = importlib.import_module

    def blocked(name, package=None):
        if name == "braintrust.api":
            raise ImportError("fixture absent SDK")
        return imported(name, package)

    monkeypatch.setattr(importlib, "import_module", blocked)
    with pytest.raises(ConfigurationError, match=r"mic-evals\[braintrust\]"):
        async with BraintrustLoader(api_key="fixture").open(
            BraintrustHandle("dataset", "pinned"), limits=ReadLimits()
        ):
            pass
