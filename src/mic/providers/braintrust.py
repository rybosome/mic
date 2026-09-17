"""Bounded, version-pinned Braintrust reads through the read-only BTQL API.

JSONL pages are consumed incrementally; no init_dataset/get-or-create operation
is used. References: https://www.braintrust.dev/docs/api-reference and
https://www.braintrust.dev/docs/kb/btql-post-endpoint-payload-and-response-schema
"""

from __future__ import annotations

import math
import os
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from mic.errors import ConfigurationError, DatasetError
from mic.models import MISSING, JsonObject, RawCase, ReadLimits

from ._braintrust.transport import ReadClient as ReadClient
from ._braintrust.transport import ReadResponse as ReadResponse
from ._braintrust.transport import create_client
from ._io import ReadBudget, parse_json


@dataclass(frozen=True)
class BraintrustHandle:
    dataset_id: str
    version: str
    api_url: str | None = None
    page_size: int = 100
    timeout: float = 30.0


def _settings(handle: BraintrustHandle, configured_key: str | None) -> tuple[str, str]:
    def nonempty_text(value: object) -> bool:
        return isinstance(value, str) and bool(value.strip())

    def positive_integer(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 1

    if not nonempty_text(handle.dataset_id):
        raise ConfigurationError("Braintrust requires an existing dataset_id")
    if not nonempty_text(handle.version) or (handle.version.lower() in {"latest", "head", "main"}):
        raise ConfigurationError("Braintrust requires an explicit pinned version (_xact_id)")
    if not positive_integer(handle.page_size):
        raise ConfigurationError("Braintrust page_size must be a positive integer")
    if isinstance(handle.timeout, bool) or not math.isfinite(handle.timeout) or handle.timeout <= 0:
        raise ConfigurationError("Braintrust timeout must be positive and finite")
    api_url = (
        handle.api_url or os.environ.get("BRAINTRUST_API_URL") or "https://api.braintrust.dev"
    ).rstrip("/")
    parsed = urlsplit(api_url)
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigurationError(
            "Braintrust api_url must be a data-plane base URL without credentials"
        )
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ConfigurationError(
            "Braintrust api_url requires HTTPS (localhost HTTP is allowed for tests)"
        )
    key = configured_key or os.environ.get("BRAINTRUST_API_KEY")
    if not key or not key.strip():
        raise ConfigurationError("Braintrust requires BRAINTRUST_API_KEY or loader api_key")
    key = key.strip()
    if any(ord(character) < 33 or ord(character) > 126 for character in key):
        # HTTP client's InvalidHeader error can include the complete token value.
        raise ConfigurationError("Braintrust API key must be one non-whitespace ASCII token")
    return api_url, key


@dataclass
class _BraintrustRead:
    client: ReadClient
    handle: BraintrustHandle
    api_url: str
    key: str = field(repr=False)
    limits: ReadLimits
    provenance: JsonObject
    used: bool = False

    def _query(self, cursor: str | None, limit: int) -> JsonObject:
        # A structured query keeps all provider identifiers/cursors as literals.
        # _pagination_key is a cursor-compatible sort in Braintrust's reference.
        return {
            "query": {
                "from": {
                    "op": "function",
                    "name": {"op": "ident", "name": ["dataset"]},
                    "args": [{"op": "literal", "value": self.handle.dataset_id}],
                },
                "select": [{"op": "star"}],
                "sort": [{"expr": {"op": "ident", "name": ["_pagination_key"]}, "dir": "asc"}],
                "limit": limit,
                "cursor": cursor,
            },
            "fmt": "jsonl",
            "version": self.handle.version,
        }

    def _row(self, data: bytes, budget: ReadBudget, page: int) -> RawCase:
        budget.add_row(len(data))
        raw = parse_json(data, f"Braintrust page {page}, row {budget.rows}")
        if not isinstance(raw, dict) or "input" not in raw:
            raise DatasetError(f"Braintrust row {budget.rows}: expected an object containing input")
        record_id = raw.get("id")
        if not isinstance(record_id, str) or not record_id:
            raise DatasetError(f"Braintrust row {budget.rows}: missing physical record id")
        provenance: JsonObject = {
            "provider": "braintrust",
            "dataset_id": self.handle.dataset_id,
            "version": self.handle.version,
            "record_id": record_id,
        }
        for key in ("_xact_id", "_pagination_key", "created"):
            if key in raw:
                provenance[key] = raw[key]
        return RawCase(
            input=raw["input"],
            expected=raw.get("expected", MISSING),
            metadata=raw.get("metadata"),
            id=record_id,
            provenance=provenance,
        )

    async def rows(self) -> AsyncIterator[object]:
        if self.used:
            raise DatasetError("Braintrust: a read may only be iterated once")
        self.used = True
        budget = ReadBudget(self.limits, "Braintrust")
        seen_cursors: set[str] = set()
        cursor: str | None = None
        page = 0
        try:
            while True:
                page += 1
                # Fetch one extra row at the cap to distinguish completion from truncation.
                page_limit = min(self.handle.page_size, self.limits.max_rows - budget.rows + 1)
                rows_in_page = 0
                async with self.client.stream(
                    "POST",
                    self.api_url + "/btql",
                    json=self._query(cursor, page_limit),
                    headers={"Authorization": f"Bearer {self.key}", "Accept": "application/jsonl"},
                    timeout=self.handle.timeout,
                ) as response:
                    if response.status_code >= 300:
                        # Do not dump credential-bearing response bodies into artifacts.
                        raise DatasetError(
                            f"Braintrust BTQL HTTP {response.status_code} on page {page}"
                        )
                    next_cursor = response.headers.get("x-bt-cursor") or response.headers.get(
                        "x-amz-meta-bt_cursor"
                    )
                    if next_cursor is not None and next_cursor in seen_cursors:
                        raise DatasetError(f"Braintrust repeated pagination cursor on page {page}")
                    buffer = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        budget.add_bytes(len(chunk))
                        buffer.extend(chunk)
                        while True:
                            end = buffer.find(b"\n")
                            if end < 0:
                                break
                            data = bytes(buffer[:end]).rstrip(b"\r")
                            del buffer[: end + 1]
                            if not data.strip():
                                continue
                            rows_in_page += 1
                            if rows_in_page > page_limit:
                                raise DatasetError(
                                    "Braintrust server exceeded requested page limit"
                                )
                            yield self._row(data, budget, page)
                        if len(buffer) > self.limits.max_record_bytes:
                            raise DatasetError(
                                f"Braintrust page {page}: "
                                f"max_record_bytes={self.limits.max_record_bytes} exceeded"
                            )
                    if buffer.strip():
                        rows_in_page += 1
                        if rows_in_page > page_limit:
                            raise DatasetError("Braintrust server exceeded requested page limit")
                        yield self._row(bytes(buffer), budget, page)
                self.provenance.update({"pages": page, "raw_bytes": budget.bytes})
                if rows_in_page == 0:
                    if next_cursor:
                        raise DatasetError(
                            "Braintrust empty page returned a cursor; pagination made no progress"
                        )
                    break
                if not next_cursor:
                    # Documented JSONL pagination provides a cursor on nonempty pages.
                    # A proxy dropping that header must not silently truncate a dataset.
                    raise DatasetError("Braintrust nonempty page is missing its pagination cursor")
                seen_cursors.add(next_cursor)
                cursor = next_cursor
        except DatasetError:
            raise
        except Exception as exc:
            raise DatasetError(f"Braintrust read failed on page {page}: {exc}") from exc


class BraintrustLoader:
    """Lazily read a pinned dataset. Injected HTTP clients remain caller-owned."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: ReadClient | None = None,
        transport: object | None = None,
    ) -> None:
        if client is not None and transport is not None:
            raise ConfigurationError("provide a Braintrust client or transport, not both")
        self._api_key = api_key
        self._client = client
        self._transport = transport

    @asynccontextmanager
    async def open(
        self, handle: BraintrustHandle, *, limits: ReadLimits
    ) -> AsyncGenerator[_BraintrustRead]:
        api_url, key = _settings(handle, self._api_key)
        if self._client is not None:
            client = self._client
        else:
            client = create_client(api_url, key, httpx_transport=self._transport)
        try:
            yield _BraintrustRead(
                client,
                handle,
                api_url,
                key,
                limits,
                {
                    "provider": "braintrust",
                    "dataset_id": handle.dataset_id,
                    "version": handle.version,
                    "api_url": api_url,
                    "page_size": handle.page_size,
                },
            )
        finally:
            if self._client is None:
                await client.aclose()
