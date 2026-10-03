"""Read context is shared across factory, source, selection, and validation."""

import asyncio
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

import mic
from mic._runtime.datasets import DatasetReader
from mic._runtime.read_context import ReadState
from tests.runtime.helpers import evaluation, row


@pytest.mark.parametrize("size,exhausted", [(2, True), (3, False), (4, False)])
def test_raw_selection_never_probes_past_count(size, exhausted):
    calls = []
    contexts = []

    @mic.dataset(input=int, expected=int)
    def data(ctx):
        contexts.append(ctx)
        try:
            for i in range(size):
                calls.append(i)
                yield {"input": i, "expected": i}
                assert ctx.rows_seen == i + 1
        finally:
            calls.append("closed")

    result = mic.run(replace(evaluation([]), dataset=data), limits=mic.ReadLimits(row_count=3))
    source = next(iter(result.sources.values()))
    assert result.exit_code == 0
    assert calls == [*range(min(size, 3)), "closed"]
    assert source.records_seen == min(size, 3)
    assert source.exhausted is exhausted
    assert contexts[0].remaining_rows == max(0, 3 - size)


def test_invalid_raw_records_consume_selection():
    @mic.dataset(input=int, expected=int)
    def data(ctx):
        yield mic.RecordError("invalid")
        yield {"input": 1, "expected": 1}
        raise AssertionError("must not read beyond selection")

    result = mic.run(
        replace(evaluation([]), dataset=data),
        limits=mic.ReadLimits(row_count=2),
        on_invalid="skip",
    )
    source = next(iter(result.sources.values()))
    assert result.exit_code == 0
    assert (source.records_seen, source.records_accepted, source.records_rejected) == (2, 1, 1)


@pytest.mark.parametrize("kind", ["sync", "coroutine", "async_generator", "awaitable"])
async def test_factory_forms_share_context_with_source(kind):
    contexts = []
    threads = []

    class Source(mic.DatasetSource):
        def read(self, ctx):
            contexts.append(ctx)
            threads.append(threading.get_ident())
            ctx.set_provenance(source=True)
            try:
                yield row()
            finally:
                threads.append(threading.get_ident())

    def sync(ctx):
        contexts.append(ctx)
        threads.append(threading.get_ident())
        ctx.set_provenance(factory=True)
        return Source()

    async def coroutine(ctx):
        return sync(ctx)

    async def async_generator(ctx):
        contexts.append(ctx)
        ctx.set_provenance(factory=True, source=True)
        yield row()

    def awaitable(ctx):
        return coroutine(ctx)

    factory = {
        "sync": sync,
        "coroutine": coroutine,
        "async_generator": async_generator,
        "awaitable": awaitable,
    }[kind]
    data = mic.dataset(input=int, expected=int)(factory)
    result = await mic.arun(replace(evaluation([]), dataset=data))
    assert result.exit_code == 0
    assert all(ctx is contexts[0] for ctx in contexts)
    assert next(iter(result.sources.values())).provenance == {"factory": True, "source": True}
    if kind == "sync":
        assert len(set(threads)) == 1


def test_context_is_read_only_and_defaults_unlimited():
    ctx = mic.ReadContext(mic.ReadLimits())
    assert ctx.remaining_rows is None and ctx.remaining_seconds is None
    assert ctx.rows_seen == 0
    for name, value in [
        ("limits", mic.ReadLimits()),
        ("rows_seen", 9),
        ("remaining_rows", 9),
        ("remaining_seconds", 9),
    ]:
        with pytest.raises(AttributeError):
            setattr(ctx, name, value)


@pytest.mark.parametrize(
    "factory", [lambda a, b: [], lambda *args: [], lambda **kwargs: [], lambda *, ctx: []]
)
def test_bad_signatures_fail_before_read(factory):
    with pytest.raises(mic.ConfigurationError, match="factory must accept"):
        mic.dataset(input=int, expected=int)(factory)


def test_body_typeerror_is_not_retried_and_provenance_survives():
    calls = []

    @mic.dataset(input=int, expected=int)
    def data(ctx):
        calls.append(ctx)
        ctx.set_provenance(started=True)
        raise TypeError("private")

    result = mic.run(replace(evaluation([]), dataset=data))
    assert result.exit_code == 2
    assert len(calls) == 1
    assert next(iter(result.sources.values())).provenance == {"started": True}


def test_budget_ticks_only_during_active_operations(monkeypatch):
    from mic import sources
    from mic._runtime import read_context

    clock = [0.0]
    fake_time = SimpleNamespace(perf_counter=lambda: clock[0])
    monkeypatch.setattr(sources, "time", fake_time)
    monkeypatch.setattr(read_context, "time", fake_time)
    state = ReadState(mic.ReadLimits(timeout_seconds=10))
    state.start()
    clock[0] += 3
    assert state.remaining_seconds == 7
    state.pause()
    clock[0] += 100
    assert state.remaining_seconds == 7
    state.start()
    clock[0] += 8
    assert state.remaining_seconds == 0
    state.pause()
    assert state.remaining_seconds == 0


async def test_factory_time_is_visible_to_source(monkeypatch):
    from mic import sources
    from mic._runtime import read_context

    clock = [0.0]
    fake_time = SimpleNamespace(perf_counter=lambda: clock[0])
    monkeypatch.setattr(sources, "time", fake_time)
    monkeypatch.setattr(read_context, "time", fake_time)

    class Source(mic.DatasetSource):
        def read(self, ctx):
            assert ctx.remaining_seconds == 7
            yield row()

    def factory(ctx):
        assert ctx.remaining_seconds == 10
        clock[0] += 3
        return Source()

    data = mic.dataset(input=int, expected=int)(factory)
    reader = DatasetReader(data, "source", mic.ReadLimits(timeout_seconds=10))
    assert len([item async for item in reader.rows()]) == 1


async def test_cancelled_factory_closes_returned_iterator_on_its_worker():
    entered = threading.Event()
    release = threading.Event()
    thread_ids = []

    class Iterator:
        def __iter__(self):
            return self

        def __next__(self):
            return row()

        def close(self):
            thread_ids.append(threading.get_ident())

    def factory(ctx):
        thread_ids.append(threading.get_ident())
        entered.set()
        release.wait(5)
        return Iterator()

    data = mic.dataset(input=int, expected=int)(factory)
    task = asyncio.create_task(mic.arun(replace(evaluation([]), dataset=data)))
    await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(thread_ids) == 2 and len(set(thread_ids)) == 1


def test_inspection_default_is_injected_and_preflight_selects():
    seen = []

    @mic.dataset(input=int, expected=int)
    def data(ctx):
        seen.append(ctx.limits)
        for i in range(30):
            yield {"input": i, "expected": i}

    assert len(mic.inspect_dataset(data)["rows"]) == 20
    assert seen[-1] == mic.ReadLimits(row_count=20)
    summary = mic.preflight(
        replace(evaluation([]), dataset=data), limits=mic.ReadLimits(row_count=2)
    )
    assert summary["dataset"]["records_seen"] == 2
    assert not summary["dataset"]["exhausted"]


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_invalid_row_counts(value):
    with pytest.raises(mic.ConfigurationError):
        mic.ReadLimits(row_count=value)


@pytest.mark.parametrize("value", [0, -1, True, float("inf"), float("nan")])
def test_invalid_time_budgets(value):
    with pytest.raises(mic.ConfigurationError):
        mic.ReadLimits(timeout_seconds=value)


def test_suite_reads_share_only_identical_dataset_contexts():
    contexts = []

    def factory(ctx):
        contexts.append(ctx)
        yield from [row(), row(2), row(3)]

    data = mic.dataset(input=int, expected=int)(factory)
    other = mic.dataset(input=int, expected=int)(factory)
    base = evaluation([])
    result = mic.run(
        [
            replace(base, dataset=data, name="a"),
            replace(base, dataset=data, name="b"),
            replace(base, dataset=other, name="c"),
        ],
        limits=mic.ReadLimits(row_count=2),
    )
    assert result.exit_code == 0
    assert len(contexts) == 2 and contexts[0] is not contexts[1]
    assert all(ctx.rows_seen == 2 for ctx in contexts)
    assert result.summary.trials.completed == 6


async def test_source_cleanup_failure_is_recorded():
    @mic.dataset(input=int, expected=int)
    def data(ctx):
        try:
            yield row()
            yield row(2)
        finally:
            raise RuntimeError("private cleanup details")

    result = await mic.arun(
        replace(evaluation([]), dataset=data), limits=mic.ReadLimits(row_count=1)
    )
    source = next(iter(result.sources.values()))
    assert result.exit_code == 2
    assert source.error.type == "RuntimeError"
    assert "private" not in source.error.message


def test_bigquery_recalculates_budget_before_each_request(monkeypatch):
    from mic import sources
    from mic._runtime import read_context
    from tests.providers.test_bigquery_provider import Client, config, handle

    clock = [0.0]
    fake_time = SimpleNamespace(perf_counter=lambda: clock[0])
    monkeypatch.setattr(sources, "time", fake_time)
    monkeypatch.setattr(read_context, "time", fake_time)

    class TimedClient(Client):
        def query(self, query, **kwargs):
            result = super().query(query, **kwargs)
            clock[0] += 1
            return result

    client = TimedClient()
    source = handle(client=client, config_factory=config)
    data = mic.dataset(input=str, expected=str)(lambda: source)
    result = mic.inspect_dataset(data, limits=mic.ReadLimits(row_count=1, timeout_seconds=10))
    assert len(result["rows"]) == 1
    assert [kwargs["timeout"] for _, kwargs in client.calls] == [10, 9]
    assert client.job.result_options == {"timeout": 8, "page_size": 1}


async def test_braintrust_recalculates_budget_and_page_size(monkeypatch):
    import httpx

    from mic import sources
    from mic._runtime import read_context
    from tests.providers.test_braintrust_provider import Fixture, handle, page

    clock = [0.0]
    fake_time = SimpleNamespace(perf_counter=lambda: clock[0])
    monkeypatch.setattr(sources, "time", fake_time)
    monkeypatch.setattr(read_context, "time", fake_time)

    class TimedFixture(Fixture):
        def __call__(self, request):
            response = super().__call__(request)
            clock[0] += 1
            return response

    fixture = TimedFixture(
        [
            page([{"id": "a", "input": 1, "expected": 1}], "a"),
            page([{"id": "b", "input": 2, "expected": 2}], "b"),
        ]
    )
    source = handle(api_key="fixture", transport=httpx.MockTransport(fixture), page_size=1)
    data = mic.dataset(input=int, expected=int)(lambda: source)
    result = await mic.arun(
        replace(evaluation([]), dataset=data),
        limits=mic.ReadLimits(row_count=2, timeout_seconds=10),
    )
    assert result.exit_code == 0
    assert len(fixture.requests) == 2
    assert [r.extensions["timeout"]["read"] for r in fixture.requests] == [10, 9]
