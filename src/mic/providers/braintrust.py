"""Bounded, version-pinned Braintrust reads through the read-only BTQL API.

JSONL pages are consumed incrementally; no init_dataset/get-or-create operation
is used. References: https://www.braintrust.dev/docs/api-reference and
https://www.braintrust.dev/docs/kb/btql-post-endpoint-payload-and-response-schema
"""

from __future__ import annotations

import math
import os
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from mic.errors import ConfigurationError, DatasetError
from mic.models import MISSING, JsonObject, RawCase
from mic.sources import DatasetSource, ReadContext

from ._braintrust.transport import ReadClient as ReadClient
from ._braintrust.transport import ReadResponse as ReadResponse
from ._braintrust.transport import create_client
from ._io import parse_json


@dataclass(frozen=True)
class BraintrustHandle(DatasetSource):
    dataset_id: str
    xact_id: str
    api_url: str | None = None
    page_size: int = 100
    timeout: float = 30.0
    api_key: str | None = field(default=None, repr=False, compare=False, kw_only=True)
    client: ReadClient | None = field(default=None, repr=False, compare=False, kw_only=True)
    transport: object | None = field(default=None, repr=False, compare=False, kw_only=True)

    async def read(self, ctx: ReadContext) -> AsyncIterator[object]:
        if self.client is not None and self.transport is not None:
            raise ConfigurationError("provide a Braintrust client or transport, not both")
        api_url, key = _settings(self, self.api_key)
        ctx.set_provenance(
            provider="braintrust",
            dataset_id=self.dataset_id,
            xact_id=self.xact_id,
            api_url=api_url,
            page_size=self.page_size,
        )
        client = self.client or create_client(api_url, key, httpx_transport=self.transport)
        reader = _BraintrustRead(client, self, api_url, key, ctx)
        iterator = reader.rows()
        try:
            async for row in iterator:
                yield row
        finally:
            try:
                await iterator.aclose()
            finally:
                if self.client is None:
                    try:
                        await client.aclose()
                    except Exception:
                        raise DatasetError("Braintrust client cleanup failed") from None


def _settings(handle: BraintrustHandle, configured_key: str | None) -> tuple[str, str]:
    def nonempty_text(value: object) -> bool:
        return isinstance(value, str) and bool(value.strip())

    def positive_integer(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 1

    if not nonempty_text(handle.dataset_id):
        raise ConfigurationError("Braintrust requires an existing dataset_id")
    if not nonempty_text(handle.xact_id) or (handle.xact_id.lower() in {"latest", "head", "main"}):
        raise ConfigurationError("Braintrust requires an explicit pinned xact_id")
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
        raise ConfigurationError("Braintrust requires BRAINTRUST_API_KEY or source api_key")
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
    ctx: ReadContext

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
            "version": self.handle.xact_id,
        }

    def _row(self, data: bytes, page: int) -> RawCase:
        self.ctx.check_record_bytes(len(data))
        raw = parse_json(data, f"Braintrust page {page}, row {self.ctx.rows_seen}")
        if not isinstance(raw, dict) or "input" not in raw:
            raise DatasetError(
                f"Braintrust row {self.ctx.rows_seen}: expected an object containing input"
            )
        record_id = raw.get("id")
        if not isinstance(record_id, str) or not record_id:
            raise DatasetError(f"Braintrust row {self.ctx.rows_seen}: missing physical record id")
        provenance: JsonObject = {
            "provider": "braintrust",
            "dataset_id": self.handle.dataset_id,
            "xact_id": self.handle.xact_id,
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

    async def rows(self) -> AsyncGenerator[object]:
        cursor: str | None = None
        page = 0
        try:
            while True:
                page += 1
                # Fetch one extra row at the cap to distinguish completion from truncation.
                page_limit = min(
                    self.handle.page_size, self.ctx.limits.max_rows - self.ctx.rows_seen + 1
                )
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
                    if next_cursor is not None and next_cursor == cursor:
                        raise DatasetError(f"Braintrust repeated pagination cursor on page {page}")
                    buffer = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        self.ctx.account_bytes(len(chunk))
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
                            yield self._row(data, page)
                        if len(buffer) > self.ctx.limits.max_record_bytes:
                            raise DatasetError(
                                f"Braintrust page {page}: "
                                f"max_record_bytes={self.ctx.limits.max_record_bytes} exceeded"
                            )
                    if buffer.strip():
                        rows_in_page += 1
                        if rows_in_page > page_limit:
                            raise DatasetError("Braintrust server exceeded requested page limit")
                        yield self._row(bytes(buffer), page)
                self.ctx.set_provenance(pages=page, raw_bytes=self.ctx.raw_bytes)
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
                cursor = next_cursor
        except DatasetError:
            raise
        except Exception:
            raise DatasetError(f"Braintrust read failed on page {page}") from None
