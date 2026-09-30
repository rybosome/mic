"""Keep the copy/paste quickstart executable without credentials or model traffic."""

import ast
import json
import re
import shlex
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mic.cli import main

ROOT = Path(__file__).resolve().parents[2]
README = (ROOT / "README.md").read_text(encoding="utf-8")
PYTHON_BLOCKS = re.findall(r"```python\n(.*?)```", README, re.DOTALL)
JSONL = re.search(r"```jsonl\n(.*?)```", README, re.DOTALL).group(1)


def test_readme_matches_executable_example_and_fixture() -> None:
    example = (ROOT / "examples/ticket_eval.py").read_text(encoding="utf-8")
    assert example.split("\n\n", 1)[1] == PYTHON_BLOCKS[0], (
        "README.md's complete Python example and examples/ticket_eval.py have drifted. "
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
    exec(compile(PYTHON_BLOCKS[0], module.__file__, "exec"), module.__dict__)
    return module, constructor, client


def test_readme_commands_and_report(quickstart, monkeypatch: pytest.MonkeyPatch) -> None:
    module, constructor, client = quickstart
    opened = MagicMock(return_value=True)
    monkeypatch.setattr("webbrowser.open", opened)
    commands = [
        line
        for block in re.findall(r"```console\n(.*?)```", README, re.DOTALL)
        for line in block.splitlines()
        if line.startswith("mic ")
    ]
    for command in commands:
        assert main(shlex.split(command)[1:]) == 0, command
    assert client.responses.parse.call_count == 33  # 3 + 15 + 15; report makes no calls.
    assert constructor.call_count == 33
    assert constructor.return_value.__exit__.call_count == 33
    constructor.assert_called_with(timeout=30, max_retries=0)
    request = client.responses.parse.call_args.kwargs
    assert request["text_format"] is module.Classification
    assert module.Ticket.model_validate_json(request["input"]).subject
    assert request["store"] is False
    assert request["model"] == "gpt-4.1-mini"
    assert "bug (broken behavior)" in request["instructions"]
    opened.assert_called_once()
    first = json.loads(Path(".mic/tickets-first/run.json").read_text())
    repeated = json.loads(Path(".mic/tickets-repeat/run.json").read_text())
    assert first["scores"]["accuracy"]["mean"] == 1
    assert first["counts"]["completed"] == 3
    assert repeated["counts"]["completed"] == 15
    assert Path(".mic/tickets-first/report.html").is_file()


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
    tree = ast.parse(PYTHON_BLOCKS[0])
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "tickets")
    lines = PYTHON_BLOCKS[0].splitlines(keepends=True)
    source = (
        "".join(lines[: function.decorator_list[0].lineno - 1])
        + PYTHON_BLOCKS[1]
        + "".join(lines[function.end_lineno :])
    )
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    assert main(["run", "ticket_eval:classify", "--output", "file"]) == 0
    native = json.loads(Path("native/run.json").read_text())
    file = json.loads(Path("file/run.json").read_text())
    assert native["dataset"]["digest"] == file["dataset"]["digest"]
    assert native["scores"] == file["scores"]


def test_wrong_labels_are_scores_not_execution_failures(quickstart) -> None:
    module, _, client = quickstart
    client.responses.parse.side_effect = lambda **kwargs: SimpleNamespace(
        output_parsed=module.Classification(label="feature")
    )
    assert main(["run", "ticket_eval:classify", "--output", "ungated"]) == 0
    assert (
        main(["run", "ticket_eval:classify", "--require", "accuracy>=0.9", "--output", "gated"])
        == 1
    )
    result = json.loads(Path("gated/run.json").read_text())
    assert result["counts"]["failed"] == 0
    assert result["scores"]["accuracy"]["mean"] == pytest.approx(1 / 3)
    assert Path("gated/report.html").is_file()


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
    assert result["counts"]["failed"] == 1
    assert result["scores"]["accuracy"]["count"] == 2
    assert constructor.return_value.__exit__.call_count == 3
    assert Path("failed/report.html").is_file()


@pytest.mark.parametrize("answer", [None, {"label": "unknown"}, {"label": 42}])
def test_missing_or_invalid_structured_output_is_execution_failure(quickstart, answer) -> None:
    module, constructor, client = quickstart
    client.responses.parse.side_effect = lambda **kwargs: SimpleNamespace(
        output_parsed=None if answer is None else module.Classification.model_validate(answer)
    )
    assert main(["run", "ticket_eval:classify", "--output", "invalid"]) == 1
    result = json.loads(Path("invalid/run.json").read_text())
    assert result["counts"]["failed"] == 3
    assert result["scores"]["accuracy"]["count"] == 0
    assert constructor.return_value.__exit__.call_count == 3


def test_invalid_dataset_rejected_before_model_call(quickstart) -> None:
    module, constructor, _ = quickstart
    # Exercise the reader's schema, not only Pydantic construction in application code.
    source = PYTHON_BLOCKS[0].replace(
        'expected=Classification(label="bug")', 'expected={"label": "unknown"}'
    )
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    assert main(["run", "ticket_eval:classify", "--output", "invalid-data"]) == 2
    constructor.assert_not_called()
