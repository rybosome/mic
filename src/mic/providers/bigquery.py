"""Read-only BigQuery source, with a mandatory dry run before execution.

The client is imported and authenticated only when opening a source. Query values
are named parameters; callers must provide a stable ORDER BY in their SQL.
"""

import hashlib
import importlib
import json
import math
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from typing import Any, Protocol, cast

from mic.errors import ConfigurationError, DatasetError
from mic.models import JsonObject, JsonValue, ReadLimits

from ._io import ReadBudget, in_thread, next_item


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
class BigQueryHandle:
    billing_project: str
    sql: str
    location: str
    maximum_bytes_billed: int = 100_000_000
    parameters: tuple[BigQueryParameter, ...] = ()
    page_size: int = 1000
    timeout: float = 60.0


class QueryJob(Protocol):
    statement_type: str | None
    total_bytes_processed: int | None
    total_bytes_billed: int | None
    job_id: str
    cache_hit: bool | None

    def result(self, *, page_size: int, timeout: float) -> Iterable[Mapping[str, object]]: ...

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


@dataclass
class _BigQueryRead:
    job: QueryJob
    handle: BigQueryHandle
    limits: ReadLimits
    provenance: JsonObject
    used: bool = False

    async def rows(self) -> AsyncIterator[object]:
        if self.used:
            raise DatasetError("BigQuery: a read may only be iterated once")
        self.used = True
        budget = ReadBudget(self.limits, "BigQuery")
        try:
            result = await in_thread(
                partial(
                    self.job.result,
                    page_size=min(self.handle.page_size, self.limits.max_rows + 1),
                    timeout=self.handle.timeout,
                )
            )
            iterator = iter(result)
            while True:
                present, row = await in_thread(lambda: next_item(iterator))
                if not present:
                    break
                if not isinstance(row, Mapping):
                    # google.cloud.bigquery.Row has .items() but is not a Mapping.
                    items = getattr(row, "items", None)
                    if not callable(items):
                        raise DatasetError("BigQuery returned a row without column mapping")
                    value = dict(cast(Iterable[tuple[str, object]], items()))
                else:
                    value = dict(row)
                # SDK-native datetime/Decimal/bytes values remain untouched for map_row.
                # This is a raw-read budget; the materializer checks canonical JSON too.
                size = len(json.dumps(value, default=str, ensure_ascii=False).encode("utf-8"))
                budget.add_bytes(size)
                budget.add_row(size)
                yield value
            self.provenance.update(
                {
                    "job_id": self.job.job_id,
                    "total_bytes_processed": self.job.total_bytes_processed,
                    "total_bytes_billed": self.job.total_bytes_billed,
                    "cache_hit": self.job.cache_hit,
                    "raw_bytes": budget.bytes,
                }
            )
        except DatasetError:
            raise
        except Exception as exc:
            raise DatasetError(f"BigQuery result read failed: {exc}") from exc


class BigQueryLoader:
    """Inject ``client`` and ``config_factory`` for SDK-free contract tests.

    Injected clients remain caller-owned. Clients produced by ``client_factory``
    are owned and always closed, including validation and query failures.
    """

    def __init__(
        self,
        *,
        client: BigQueryClient | None = None,
        client_factory: ClientFactory = _client,
        config_factory: ConfigFactory = _config,
    ) -> None:
        self._client = client
        self._client_factory = client_factory
        self._config_factory = config_factory

    async def _dry_run(self, client: BigQueryClient, handle: BigQueryHandle) -> QueryJob:
        config = self._config_factory(handle, True)
        job = await in_thread(
            partial(
                client.query,
                handle.sql,
                job_config=config,
                location=handle.location,
                timeout=handle.timeout,
            )
        )
        _check_dry_run(handle, job)
        return job

    async def estimate(self, handle: BigQueryHandle) -> JsonObject:
        """Dry-run cost metadata; never submit an executable query."""
        _validate(handle)
        client = self._client or await in_thread(
            lambda: self._client_factory(handle), on_cancel=lambda created: created.close()
        )
        try:
            return _provenance(handle, await self._dry_run(client, handle))
        except (DatasetError, ConfigurationError):
            raise
        except Exception as exc:
            raise DatasetError(f"BigQuery dry run failed: {exc}") from exc
        finally:
            if self._client is None:
                await in_thread(client.close)

    @asynccontextmanager
    async def open(
        self, handle: BigQueryHandle, *, limits: ReadLimits
    ) -> AsyncGenerator[_BigQueryRead]:
        _validate(handle)
        client = self._client or await in_thread(
            lambda: self._client_factory(handle), on_cancel=lambda created: created.close()
        )
        job: QueryJob | None = None
        completed = False
        try:
            dry_job = await self._dry_run(client, handle)
            provenance = _provenance(handle, dry_job)
            config = self._config_factory(handle, False)
            job = await in_thread(
                partial(
                    client.query,
                    handle.sql,
                    job_config=config,
                    location=handle.location,
                    timeout=handle.timeout,
                ),
                on_cancel=lambda submitted: submitted.cancel(),
            )
            provenance["job_id"] = job.job_id
            yield _BigQueryRead(job, handle, limits, provenance)
            completed = True
        except (DatasetError, ConfigurationError):
            raise
        except Exception as exc:
            raise DatasetError(f"BigQuery dataset read failed: {exc}") from exc
        finally:
            if not completed and job is not None:
                try:
                    await in_thread(job.cancel)
                except Exception:
                    pass  # Preserve the original read failure; cancellation is best effort.
            if self._client is None:
                await in_thread(client.close)
