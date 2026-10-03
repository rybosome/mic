"""Read-only BigQuery source, with a mandatory dry run before execution.

The client is imported and authenticated only when opening a source. Query values
are named parameters; callers must provide a stable ORDER BY in their SQL.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from mic.errors import ConfigurationError, DatasetError
from mic.models import JsonObject, JsonValue
from mic.sources import DatasetSource, ReadContext


@dataclass(frozen=True)
class BigQueryParameter:
    """A named scalar or array parameter using BigQuery's Standard SQL type names.

    Examples: ``BigQueryParameter('team', 'STRING', 'support')`` and
    ``BigQueryParameter('ids', 'ARRAY<INT64>', [1, 2])``. Date/decimal values may
    use SDK-supported strings. STRUCT parameters are deliberately not inferred.
    """

    name: str
    sql_type: str
    value: JsonValue


@dataclass(frozen=True)
class BigQueryHandle(DatasetSource):
    billing_project: str
    sql: str
    location: str
    maximum_bytes_billed: int = 100_000_000
    parameters: tuple[BigQueryParameter, ...] = ()
    page_size: int = 1000
    timeout: float = 60.0
    client: BigQueryClient | None = field(default=None, repr=False, compare=False, kw_only=True)
    client_factory: ClientFactory | None = field(
        default=None, repr=False, compare=False, kw_only=True
    )
    config_factory: ConfigFactory | None = field(
        default=None, repr=False, compare=False, kw_only=True
    )

    def read(self, ctx: ReadContext) -> Iterator[object]:
        _validate(self)
        config = self.config_factory or _config
        client = self.client or (self.client_factory or _client)(self)
        job: QueryJob | None = None
        exhausted = False
        try:
            dry = client.query(
                self.sql,
                job_config=config(self, True),
                location=self.location,
                timeout=self.timeout,
            )
            _check_dry_run(self, dry)
            ctx.set_provenance(**_provenance(self, dry))
            job = client.query(
                self.sql,
                job_config=config(self, False),
                location=self.location,
                timeout=self.timeout,
            )
            ctx.set_provenance(job_id=job.job_id)
            rows = job.result(
                page_size=min(self.page_size, ctx.limits.max_rows + 1), timeout=self.timeout
            )
            for row in rows:
                if isinstance(row, Mapping):
                    value = dict(cast(Mapping[str, object], row))
                else:
                    # The SDK's Row exposes items() but is not a Mapping.
                    items = getattr(row, "items", None)
                    if not callable(items):
                        raise DatasetError("BigQuery returned a row without column mapping")
                    value = dict(cast(Iterable[tuple[str, object]], items()))
                # Preserve native Decimal/datetime/bytes for the user's mapper.
                # This bounds a decoded representation, not HTTP wire bytes.
                size = len(json.dumps(value, default=str, ensure_ascii=False).encode("utf-8"))
                ctx.account_bytes(size)
                ctx.check_record_bytes(size)
                yield value
            exhausted = True
            ctx.set_provenance(
                total_bytes_processed=job.total_bytes_processed,
                total_bytes_billed=job.total_bytes_billed,
                cache_hit=job.cache_hit,
                raw_bytes=ctx.raw_bytes,
            )
        except (DatasetError, ConfigurationError):
            raise
        except Exception:
            # SDK exception text can contain query values and response bodies.
            raise DatasetError("BigQuery dataset read failed") from None
        finally:
            if job is not None and not exhausted:
                with suppress(Exception):
                    job.cancel()
            if self.client is None:
                try:
                    client.close()
                except Exception:
                    raise DatasetError("BigQuery client cleanup failed") from None


class QueryJob(Protocol):
    statement_type: str | None
    total_bytes_processed: int | None
    total_bytes_billed: int | None
    job_id: str
    cache_hit: bool | None

    def result(self, *, page_size: int, timeout: float) -> Iterable[object]: ...

    def cancel(self) -> bool: ...


class BigQueryClient(Protocol):
    def query(
        self, query: str, *, job_config: object, location: str, timeout: float
    ) -> QueryJob: ...

    def close(self) -> None: ...


type ConfigFactory = Callable[[BigQueryHandle, bool], object]
type ClientFactory = Callable[[BigQueryHandle], BigQueryClient]


def _sdk() -> Any:
    # Any is confined to the optional SDK boundary, whose classes vary by version.
    try:
        return importlib.import_module("google.cloud.bigquery")
    except ImportError as exc:
        raise ConfigurationError("BigQuery requires the 'mic-evals[bigquery]' extra") from exc


def _client(handle: BigQueryHandle) -> BigQueryClient:
    return cast(
        BigQueryClient, _sdk().Client(project=handle.billing_project, location=handle.location)
    )


def _config(handle: BigQueryHandle, dry_run: bool) -> object:
    sdk = _sdk()
    parameters: list[object] = []
    for param in handle.parameters:
        type_name = param.sql_type.upper()
        if type_name.startswith("ARRAY<") and type_name.endswith(">"):
            parameters.append(sdk.ArrayQueryParameter(param.name, type_name[6:-1], param.value))
        else:
            parameters.append(sdk.ScalarQueryParameter(param.name, type_name, param.value))
    return sdk.QueryJobConfig(
        dry_run=dry_run,
        use_legacy_sql=False,
        use_query_cache=False,
        maximum_bytes_billed=handle.maximum_bytes_billed,
        query_parameters=parameters,
    )


def _validate(handle: BigQueryHandle) -> None:
    for name in ("billing_project", "sql", "location"):
        value = getattr(handle, name)
        if not isinstance(value, str) or not value.strip():
            raise ConfigurationError(f"BigQuery {name} must be non-empty")
    for name in ("maximum_bytes_billed", "page_size"):
        value = getattr(handle, name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ConfigurationError(f"BigQuery {name} must be a positive integer")
    if isinstance(handle.timeout, bool) or not math.isfinite(handle.timeout) or handle.timeout <= 0:
        raise ConfigurationError("BigQuery timeout must be positive and finite")
    names: set[str] = set()
    scalar_types = {
        "STRING",
        "INT64",
        "FLOAT64",
        "BOOL",
        "BOOLEAN",
        "BYTES",
        "DATE",
        "DATETIME",
        "TIME",
        "TIMESTAMP",
        "NUMERIC",
        "BIGNUMERIC",
        "JSON",
    }
    for parameter in handle.parameters:
        if not parameter.name.isidentifier() or parameter.name in names:
            raise ConfigurationError("BigQuery parameters require unique identifier names")
        names.add(parameter.name)
        type_name = parameter.sql_type.upper()
        if type_name.startswith("ARRAY<") and type_name.endswith(">"):
            if not isinstance(parameter.value, list):
                raise ConfigurationError(
                    f"BigQuery array parameter {parameter.name} requires a list"
                )
            type_name = type_name[6:-1]
        if type_name not in scalar_types:
            raise ConfigurationError(f"unsupported BigQuery parameter type {parameter.sql_type!r}")
        try:
            json.dumps(parameter.value, allow_nan=False)
        except (ValueError, TypeError) as exc:
            raise ConfigurationError(
                f"BigQuery parameter {parameter.name} must be finite JSON"
            ) from exc


def _provenance(handle: BigQueryHandle, dry_job: QueryJob) -> JsonObject:
    parameters: list[JsonValue] = [
        {"name": p.name, "sql_type": p.sql_type.upper(), "value": p.value}
        for p in handle.parameters
    ]
    identity: JsonObject = {
        "sql": handle.sql,
        "parameters": parameters,
        "billing_project": handle.billing_project,
        "location": handle.location,
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return {
        "provider": "bigquery",
        **identity,
        "query_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
        "maximum_bytes_billed": handle.maximum_bytes_billed,
        "estimated_bytes_processed": dry_job.total_bytes_processed,
        "statement_type": dry_job.statement_type,
    }


def _check_dry_run(handle: BigQueryHandle, job: QueryJob) -> None:
    if job.statement_type != "SELECT":
        raise DatasetError(
            f"BigQuery requires one read-only SELECT; dry run returned {job.statement_type!r}"
        )
    estimated = job.total_bytes_processed
    if estimated is not None and estimated > handle.maximum_bytes_billed:
        raise DatasetError(
            f"BigQuery dry run estimates {estimated} bytes, exceeding "
            f"maximum_bytes_billed={handle.maximum_bytes_billed}"
        )
