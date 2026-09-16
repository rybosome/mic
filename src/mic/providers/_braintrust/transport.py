"""Optional SDK transport boundary for Braintrust's bounded JSONL reader."""

import importlib
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from functools import partial
from typing import Protocol, cast

from mic.errors import ConfigurationError
from mic.models import JsonObject

from .._io import in_thread, next_item


class ReadResponse(Protocol):
    """The minimal streaming response contract; HTTPX responses also implement it."""

    @property
    def status_code(self) -> int: ...

    @property
    def headers(self) -> Mapping[str, str]: ...

    def aiter_bytes(self, chunk_size: int) -> AsyncIterator[bytes]: ...


class ReadClient(Protocol):
    """Injectable async transport boundary, independent of a specific HTTP library."""

    def stream(
        self,
        method: str,
        url: str,
        *,
        json: JsonObject,
        headers: Mapping[str, str],
        timeout: float,
    ) -> AbstractAsyncContextManager[ReadResponse]: ...

    async def aclose(self) -> None: ...


class _SDKResponse(Protocol):
    @property
    def status_code(self) -> int: ...

    @property
    def headers(self) -> Mapping[str, str]: ...

    def iter_content(self, chunk_size: int) -> Iterator[bytes]: ...

    def close(self) -> None: ...


class _SDKSession(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        json: JsonObject,
        headers: Mapping[str, str],
        timeout: float,
        stream: bool,
        allow_redirects: bool,
        auth: Callable[[object], object],
    ) -> _SDKResponse: ...


class _SDKTransport(Protocol):
    @property
    def session(self) -> _SDKSession: ...


class _SDKClient(Protocol):
    @property
    def transport(self) -> _SDKTransport: ...

    def close(self) -> None: ...


@dataclass
class _AsyncSDKResponse:
    response: _SDKResponse

    @property
    def status_code(self) -> int:
        return self.response.status_code

    @property
    def headers(self) -> Mapping[str, str]:
        return self.response.headers

    async def aiter_bytes(self, chunk_size: int) -> AsyncIterator[bytes]:
        iterator = self.response.iter_content(chunk_size=chunk_size)
        while True:
            present, chunk = await in_thread(lambda: next_item(iterator))
            if not present:
                break
            if chunk:
                yield chunk


class _BraintrustSDKReadClient:
    """Use the pinned SDK's owned Session without its buffering dataset helpers.

    The 0.39.0 high-level dataset fetcher accumulates pages. Its legacy long-lived
    HTTP adapter also eagerly reads response.content. The SDK's ordinary client
    Session supports bounded streaming without either behavior. The session is
    accessed through the pinned client and stays under that client's ownership.
    """

    def __init__(self, api_url: str, key: str) -> None:
        try:
            sdk = importlib.import_module("braintrust.api")
        except ImportError as exc:
            raise ConfigurationError(
                "Braintrust source requires the 'mic-evals[braintrust]' extra"
            ) from exc
        # The optional SDK boundary is structural: no optional types leak to core.
        self._client = cast(
            _SDKClient,
            sdk.BraintrustClient(
                api_key=key,
                api_url=api_url,
                enable_sdk_retries=False,
            ),
        )

    @asynccontextmanager
    async def stream(
        self,
        method: str,
        url: str,
        *,
        json: JsonObject,
        headers: Mapping[str, str],
        timeout: float,
    ) -> AsyncGenerator[ReadResponse]:
        response = await in_thread(
            partial(
                self._client.transport.session.request,
                method,
                url,
                json=json,
                headers=headers,
                timeout=timeout,
                stream=True,
                allow_redirects=False,
                # Keep the explicit API key authoritative instead of consulting
                # unrelated .netrc credentials during requests preparation.
                auth=lambda request: request,
            ),
            on_cancel=lambda opened: opened.close(),
        )
        try:
            yield _AsyncSDKResponse(response)
        finally:
            await in_thread(response.close)

    async def aclose(self) -> None:
        await in_thread(self._client.close)


def create_client(api_url: str, key: str, *, httpx_transport: object | None = None) -> ReadClient:
    if httpx_transport is None:
        return _BraintrustSDKReadClient(api_url, key)
    # Explicit HTTPX injection is useful for tests; the default SDK path does not
    # import HTTPX or require it as a runtime dependency.
    try:
        httpx_module = importlib.import_module("httpx")
    except ImportError as exc:
        raise ConfigurationError(
            "Explicit HTTPX transport injection requires httpx; "
            "the default Braintrust client only requires braintrust"
        ) from exc
    return cast(
        ReadClient,
        httpx_module.AsyncClient(
            transport=httpx_transport,
            follow_redirects=False,
        ),
    )
