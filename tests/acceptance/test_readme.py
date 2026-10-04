"""Keep the copy/paste quickstart executable without credentials or model traffic."""

import ast
import asyncio
import json
import re
import shlex
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from mic.cli import main

ROOT = Path(__file__).resolve().parents[2]
README = (ROOT / "README.md").read_text(encoding="utf-8")


def snippet(name: str) -> str:
    match = re.search(
        rf"<!-- snippet: {re.escape(name)} -->\s*```(?:python|jsonl|console)\n(.*?)```",
        README,
        re.DOTALL,
    )
    assert match is not None, f"Missing README snippet: {name}"
    return match.group(1)


QUICKSTART = "\n\n".join(
    snippet(name) for name in ("quickstart-dataset", "quickstart-scoring", "quickstart-task")
)
JSONL = snippet("tickets-jsonl")


def test_readme_matches_executable_example_and_fixture() -> None:
    example = (ROOT / "examples/ticket_eval.py").read_text(encoding="utf-8")
    assert example.split("\n\n", 1)[1] == QUICKSTART, (
        "README.md's assembled quickstart and examples/ticket_eval.py have drifted. "
        "Update both together, keeping the example file's module docstring."
    )
    assert (ROOT / "examples/fixtures/tickets.jsonl").read_text(encoding="utf-8") == JSONL


@pytest.fixture
def quickstart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Never import a real SDK or use the host's credentials during this test.
    sdk = ModuleType("openai")
    constructor = MagicMock()
    sdk.OpenAI = constructor
    monkeypatch.setitem(sys.modules, "openai", sdk)
    client = constructor.return_value.__enter__.return_value
    labels = {row["input"]["body"]: row["expected"] for row in map(json.loads, JSONL.splitlines())}
    client.responses.parse.side_effect = lambda **kwargs: SimpleNamespace(
        output_parsed=kwargs["text_format"](**labels[json.loads(kwargs["input"])["body"]])
    )
    module = ModuleType("ticket_eval")
    module.__file__ = str(tmp_path / "ticket_eval.py")
    monkeypatch.setitem(sys.modules, "ticket_eval", module)
    exec(compile(QUICKSTART, module.__file__, "exec"), module.__dict__)
    return module, constructor, client


def test_readme_commands_and_report(quickstart, monkeypatch: pytest.MonkeyPatch) -> None:
    module, constructor, client = quickstart
    task = snippet("multiple-scorers") + snippet("quickstart-task").split("\n", 1)[1]
    exec(snippet("extended-scoring") + "\n\n" + task, module.__dict__)
    opened = MagicMock(return_value=True)
    monkeypatch.setattr("webbrowser.open", opened)
    commands = [
        line
        for block in re.findall(r"```console\n(.*?)```", README, re.DOTALL)
        for line in block.replace("\\\n", " ").splitlines()
        if line.startswith("mic ")
    ]
    for command in commands:
        if "--help" in command:
            with pytest.raises(SystemExit) as result:
                main(shlex.split(command)[1:])
            assert result.value.code == 0
        else:
            assert main(shlex.split(command)[1:]) == 0, command
    assert (
        client.responses.parse.call_count == 36
    )  # 3 + 15 + 15 + 3; discovery/inspection/preflight/report/help make no model calls.
    assert constructor.call_count == 36
    assert constructor.return_value.__exit__.call_count == 36
    constructor.assert_called_with(timeout=30, max_retries=0)
    request = client.responses.parse.call_args.kwargs
    assert request["text_format"] is module.Classification
    assert module.Ticket.model_validate_json(request["input"]).subject
    assert request["store"] is False
    assert request["model"] == "gpt-4.1-mini"
    assert "bug (broken behavior)" in request["instructions"]
    assert opened.call_count == 2
    first = json.loads(Path(".mic/tickets/run.json").read_text())
    repeated = json.loads(Path(".mic/tickets-v2/run.json").read_text())
    assert first["summary"]["tasks"]["classify"]["scores"]["accuracy"]["mean"] == 1
    assert first["summary"]["trials"]["completed"] == 3
    assert repeated["summary"]["trials"]["completed"] == 15
    assert repeated["info"]["tasks"]["classify"]["options"]["trials"] == 5
    assert repeated["info"]["tasks"]["classify"]["options"]["concurrency"] == 2
    assert repeated["info"]["tasks"]["classify"]["options"]["timeout"] == 30
    assert Path(".mic/tickets/report.html").is_file()


def test_discovery_and_preflight_do_not_create_model_client(quickstart) -> None:
    _, constructor, _ = quickstart
    assert main(["list", "ticket_eval"]) == 0
    assert main(["preflight", "ticket_eval:classify"]) == 0
    constructor.assert_not_called()


def test_jsonl_replacement_preserves_dataset_and_scores(quickstart, tmp_path: Path) -> None:
    module, _, _ = quickstart
    assert main(["run", "ticket_eval:classify", "--output", "native"]) == 0
    (tmp_path / "tickets.jsonl").write_text(JSONL, encoding="utf-8")
    # Apply the README's replacement before its evaluation decorator binds the dataset.
    tree = ast.parse(QUICKSTART)
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "tickets")
    lines = QUICKSTART.splitlines(keepends=True)
    source = (
        "".join(lines[: function.decorator_list[0].lineno - 1])
        + snippet("file-dataset")
        + "".join(lines[function.end_lineno :])
    )
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    assert main(["run", "ticket_eval:classify", "--output", "file"]) == 0
    native = json.loads(Path("native/run.json").read_text())
    file = json.loads(Path("file/run.json").read_text())
    assert (
        next(iter(native["sources"].values()))["digest"]
        == next(iter(file["sources"].values()))["digest"]
    )
    assert (
        native["summary"]["tasks"]["classify"]["scores"]
        == file["summary"]["tasks"]["classify"]["scores"]
    )


def test_wrong_labels_are_scores_not_execution_failures(quickstart) -> None:
    module, _, client = quickstart
    client.responses.parse.side_effect = lambda **kwargs: SimpleNamespace(
        output_parsed=module.Classification(label="feature")
    )
    assert main(["run", "ticket_eval:classify", "--output", "ungated"]) == 0
    assert (
        main(
            [
                "run",
                "ticket_eval:classify",
                "--require",
                'tasks["classify"].scores["accuracy"].mean>=0.9',
                "--output",
                "gated",
            ]
        )
        == 1
    )
    result = json.loads(Path("gated/run.json").read_text())
    assert result["summary"]["trials"]["task_failed"] == 0
    assert result["summary"]["tasks"]["classify"]["scores"]["accuracy"]["mean"] == pytest.approx(
        1 / 3
    )
    assert Path("gated/events.jsonl").is_file()


def test_model_failure_keeps_other_results_and_closes_clients(quickstart) -> None:
    _, constructor, client = quickstart
    respond = client.responses.parse.side_effect

    def fail_one(**kwargs):
        if "PDF" in json.loads(kwargs["input"])["body"]:
            raise RuntimeError("Synthetic model failure")
        return respond(**kwargs)

    client.responses.parse.side_effect = fail_one
    assert main(["run", "ticket_eval:classify", "--output", "failed"]) == 1
    result = json.loads(Path("failed/run.json").read_text())
    assert result["summary"]["trials"]["task_failed"] == 1
    assert result["summary"]["tasks"]["classify"]["scores"]["accuracy"]["count"] == 2
    assert constructor.return_value.__exit__.call_count == 3
    assert Path("failed/events.jsonl").is_file()


@pytest.mark.parametrize("answer", [None, {"label": "unknown"}, {"label": 42}])
def test_missing_or_invalid_structured_output_is_execution_failure(quickstart, answer) -> None:
    module, constructor, client = quickstart
    client.responses.parse.side_effect = lambda **kwargs: SimpleNamespace(
        output_parsed=None if answer is None else module.Classification.model_validate(answer)
    )
    assert main(["run", "ticket_eval:classify", "--output", "invalid"]) == 1
    result = json.loads(Path("invalid/run.json").read_text())
    assert result["summary"]["trials"]["task_failed"] == 3
    assert result["summary"]["tasks"]["classify"]["scores"]["accuracy"]["count"] == 0
    assert constructor.return_value.__exit__.call_count == 3


def test_invalid_dataset_rejected_before_model_call(quickstart) -> None:
    module, constructor, _ = quickstart
    # Exercise the reader's schema, not only Pydantic construction in application code.
    source = QUICKSTART.replace(
        'expected=Classification(label="bug")', 'expected={"label": "unknown"}'
    )
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    assert main(["run", "ticket_eval:classify", "--output", "invalid-data"]) == 2
    constructor.assert_not_called()


def test_dataclass_schemas_hydrate_the_same_ticket_records(quickstart) -> None:
    from dataclasses import is_dataclass

    module, constructor, _ = quickstart
    source = (
        "import mic\n"
        + snippet("dataclasses")
        + "\n@mic.dataset"
        + snippet("quickstart-dataset").split("@mic.dataset", 1)[1]
    )
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    record = json.loads(JSONL.splitlines()[0])
    ticket = module.tickets.schema.input.validate(record["input"])
    expected = module.tickets.schema.expected.validate(record["expected"])
    assert is_dataclass(ticket) and is_dataclass(expected)
    assert ticket.subject == record["input"]["subject"]
    assert expected.label == "bug"
    assert main(["inspect", "ticket_eval:tickets", "--limit", "3"]) == 0
    constructor.assert_not_called()


@pytest.mark.parametrize("name", ["bigquery-dataset", "braintrust-dataset"])
def test_cloud_factory_examples_are_passive(quickstart, name: str) -> None:
    from mic.providers.bigquery import BigQueryHandle
    from mic.providers.braintrust import BraintrustHandle

    module, constructor, _ = quickstart
    exec(compile(snippet(name), module.__file__, "exec"), module.__dict__)
    handle = module.tickets.factory()
    if name == "bigquery-dataset":
        assert isinstance(handle, BigQueryHandle)
        assert handle.maximum_bytes_billed == 10_000_000
        assert "STRUCT(subject, body) AS input" in handle.sql
        assert "STRUCT(label) AS expected" in handle.sql
        assert "ORDER BY id" in handle.sql
    else:
        assert isinstance(handle, BraintrustHandle)
        assert handle.dataset_id == "your-dataset-id"
        assert handle.xact_id == "your-pinned-version"
    constructor.assert_not_called()


@pytest.mark.parametrize("miss_bug,relaxed_accuracy", [(False, False), (True, False), (True, True)])
def test_multiple_metrics_metadata_and_nonapplicable_scores(
    quickstart, miss_bug: bool, relaxed_accuracy: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    module, _, client = quickstart
    task = snippet("quickstart-task")
    task = snippet("multiple-scorers") + task.split("\n", 1)[1]
    source = snippet("quickstart-dataset") + "\n\n" + snippet("extended-scoring") + "\n\n" + task
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    respond = client.responses.parse.side_effect

    def predict(**kwargs):
        if miss_bug and json.loads(kwargs["input"])["subject"] == "PDF upload":
            return SimpleNamespace(output_parsed=module.Classification(label="question"))
        return respond(**kwargs)

    client.responses.parse.side_effect = predict
    command = shlex.split(snippet("cli-gates").replace("\\\n", " "))[1:]
    if relaxed_accuracy:
        command[command.index('tasks["classify"].scores["accuracy"].mean>=0.9')] = (
            'tasks["classify"].scores["accuracy"].mean>=0.6'
        )
    assert main([*command, "--output", "metrics", "--json"]) == (1 if miss_bug else 0)
    manifest = json.loads(Path("metrics/run.json").read_text())
    assert json.loads(capsys.readouterr().out) == manifest
    assert [gate["passed"] for gate in manifest["requirements"]] == [
        not miss_bug or relaxed_accuracy,
        not miss_bug,
        True,
        True,
        True,
    ]
    assert manifest["summary"]["trials"]["completed"] == 15
    assert manifest["summary"]["trials"]["task_failed"] == 0
    assert Path("metrics/events.jsonl").is_file()
    assert manifest["summary"]["tasks"]["classify"]["scores"]["accuracy"]["mean"] == pytest.approx(
        2 / 3 if miss_bug else 1
    )
    recall = manifest["summary"]["tasks"]["classify"]["scores"]["bug_recall"]
    assert recall["count"] == 5 and manifest["summary"]["trials"]["scoring_skipped"] == 10
    assert recall["mean"] == (0 if miss_bug else 1)
    cases = [
        e["result"]
        for line in Path("metrics/events.jsonl").read_text().splitlines()
        if (e := json.loads(line))["type"] == "trial_finished"
    ]
    bug = next(case for case in cases if case["label"] == "upload")
    assert bug["scores"][0]["metadata"] == {
        "body_length": len("The app closes whenever I upload a PDF."),
    }


@pytest.mark.parametrize("with_context", [False, True])
@pytest.mark.parametrize("missing_output", [False, True])
def test_async_task_and_context_examples(quickstart, with_context, missing_output, caplog) -> None:
    module, _, sync_client = quickstart
    constructor = MagicMock()
    sys.modules["openai"].AsyncOpenAI = constructor
    client = constructor.return_value.__aenter__.return_value
    client.responses.parse = AsyncMock(
        side_effect=(
            (lambda **kwargs: SimpleNamespace(output_parsed=None))
            if missing_output
            else sync_client.responses.parse.side_effect
        )
    )
    source = snippet("async-task")
    if with_context:
        source = source.replace(
            "async def classify(ticket: Ticket) -> Classification:\n",
            snippet("task-context"),
        )
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    caplog.set_level("INFO", logger="ticket_eval")
    assert main(["run", "ticket_eval:classify", "--trials", "2", "--output", "async"]) == (
        1 if missing_output else 0
    )
    assert client.responses.parse.await_count == 6
    assert constructor.return_value.__aexit__.await_count == 6
    constructor.assert_called_with(timeout=30, max_retries=0)
    request = client.responses.parse.call_args.kwargs
    assert request["text_format"] is module.Classification
    assert request["store"] is False
    result = json.loads(Path("async/run.json").read_text())
    assert result["summary"]["trials"]["task_failed"] == (6 if missing_output else 0)
    if with_context:
        assert {record.getMessage() for record in caplog.records} == {
            f"case={case} trial={trial}"
            for case in (f"{result['run_id']}:s0:r{i}" for i in range(3))
            for trial in (1, 2)
        }


@pytest.mark.parametrize("async_runner", [False, True])
@pytest.mark.parametrize("wrong_labels", [False, True])
def test_programmatic_invocation_examples(quickstart, async_runner, wrong_labels) -> None:
    module, _, client = quickstart
    if wrong_labels:
        client.responses.parse.side_effect = lambda **kwargs: SimpleNamespace(
            output_parsed=module.Classification(label="feature")
        )
    namespace = {}
    if async_runner:
        code = compile(
            snippet("programmatic-arun"),
            "readme_notebook",
            "exec",
            flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
        )
        asyncio.run(eval(code, namespace))
    else:
        with pytest.raises(SystemExit) as outcome:
            exec(snippet("programmatic-run"), namespace)
        assert outcome.value.code == (1 if wrong_labels else 0)
    result = namespace["result"]
    assert result.exit_code == (1 if wrong_labels else 0)
    assert result.summary.trials.completed == 15
    assert result.info.tasks["classify"]["options"]["concurrency"] == 2
    assert result.output_dir is None


def test_yaml_source_example_uses_public_streaming_api(quickstart, tmp_path):
    module, constructor, _ = quickstart
    # JSON documents are also valid YAML; reuse the same classifier fixture.
    (tmp_path / "tickets.yaml").write_text(
        "\n---\n".join(JSONL.splitlines()) + "\n", encoding="utf-8"
    )
    exec(snippet("yaml-dataset"), module.__dict__)
    exec(snippet("quickstart-task"), module.__dict__)
    assert main(["run", "ticket_eval:classify", "--output", "yaml"]) == 0
    saved = json.loads(Path("yaml/run.json").read_text(encoding="utf-8"))
    assert saved["summary"]["trials"]["completed"] == 3
    assert constructor.call_count == 3
    assert next(iter(saved["sources"].values()))["provenance"] == {}


def test_inline_yaml_source_is_lazy_and_closes_on_early_exit(quickstart, tmp_path, monkeypatch):

    module, _, _ = quickstart
    exec(snippet("yaml-dataset"), module.__dict__)
    path = tmp_path / "tickets.yaml"
    path.write_text("input: first\n---\ninput: second\n", encoding="utf-8")
    opened = []
    original_open = Path.open

    def track_open(self, *args, **kwargs):
        stream = original_open(self, *args, **kwargs)
        opened.append(stream)
        return stream

    monkeypatch.setattr(Path, "open", track_open)
    records = module.tickets.factory()
    assert opened == []
    assert next(records) == {"input": "first"}
    assert len(opened) == 1
    assert not opened[0].closed
    records.close()
    assert opened[0].closed


def test_inline_yaml_source_sanitizes_parser_errors(quickstart, tmp_path):
    import mic

    module, _, _ = quickstart
    exec(snippet("yaml-dataset"), module.__dict__)
    path = tmp_path / "tickets.yaml"
    path.write_text("input: [private source excerpt\n", encoding="utf-8")
    records = module.tickets.factory()
    with pytest.raises(mic.DatasetError, match="^Invalid YAML document stream$"):
        list(records)


@pytest.mark.parametrize("packaged", [False, True])
def test_contextual_yaml_examples_select_records_and_forward_timeout(
    quickstart, monkeypatch, packaged
):
    import urllib.request
    from io import BytesIO

    import mic

    module, _, _ = quickstart
    opened = []

    def open_yaml(url, *, timeout):
        assert url == "https://example.com/tickets.yaml"
        assert 0 < timeout <= 30
        stream = BytesIO(("\n---\n".join(JSONL.splitlines()) + "\n").encode())
        opened.append(stream)
        return stream

    monkeypatch.setattr(urllib.request, "urlopen", open_yaml)
    exec(snippet("yaml-dataset"), module.__dict__)
    exec(snippet("yaml-context-dataset"), module.__dict__)
    if packaged:
        exec(snippet("yaml-source-dataset"), module.__dict__)
        source = module.tickets.factory()
        assert isinstance(source, mic.DatasetSource)
    assert opened == []
    inspected = mic.inspect_dataset(
        module.tickets, limits=mic.ReadLimits(row_count=2, timeout_seconds=30)
    )
    assert len(inspected["rows"]) == 2
    assert inspected["dataset"]["records_seen"] == 2
    assert not inspected["dataset"]["exhausted"]
    assert inspected["dataset"]["provenance"]["provider"] == "yaml"
    assert len(opened) == 1 and opened[0].closed
