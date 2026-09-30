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
    assert example.split("\n\n", 1)[1] == PYTHON_BLOCKS[0]
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
    labels = {row["input"]: row["expected"] for row in map(json.loads, JSONL.splitlines())}
    client.responses.create.side_effect = lambda **kwargs: SimpleNamespace(
        output_text=f" {labels[kwargs['input']]}\n"
    )
    module = ModuleType("ticket_eval")
    module.__file__ = str(tmp_path / "ticket_eval.py")
    monkeypatch.setitem(sys.modules, "ticket_eval", module)
    exec(compile(PYTHON_BLOCKS[0], module.__file__, "exec"), module.__dict__)
    return module, constructor, client


def test_readme_commands_and_report(quickstart, monkeypatch: pytest.MonkeyPatch) -> None:
    _, constructor, client = quickstart
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
    assert client.responses.create.call_count == 33  # 3 + 15 + 15; report makes no calls.
    assert constructor.call_count == 33
    assert constructor.return_value.__exit__.call_count == 33
    constructor.assert_called_with(timeout=30, max_retries=0)
    request = client.responses.create.call_args.kwargs
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


@pytest.mark.parametrize("answer", ["wrong", "bug: broken behavior", ""])
def test_bad_answers_are_scores_not_execution_failures(quickstart, answer: str) -> None:
    _, _, client = quickstart
    client.responses.create.side_effect = lambda **kwargs: SimpleNamespace(output_text=answer)
    assert main(["run", "ticket_eval:classify", "--output", "ungated"]) == 0
    assert (
        main(["run", "ticket_eval:classify", "--require", "accuracy>=0.9", "--output", "gated"])
        == 1
    )
    result = json.loads(Path("gated/run.json").read_text())
    assert result["counts"]["failed"] == 0
    assert result["scores"]["accuracy"]["mean"] == 0
    assert Path("gated/report.html").is_file()


def test_model_failure_keeps_other_results_and_closes_clients(quickstart) -> None:
    _, constructor, client = quickstart
    respond = client.responses.create.side_effect

    def fail_one(**kwargs):
        if "PDF" in kwargs["input"]:
            raise RuntimeError("Synthetic model failure")
        return respond(**kwargs)

    client.responses.create.side_effect = fail_one
    assert main(["run", "ticket_eval:classify", "--output", "failed"]) == 1
    result = json.loads(Path("failed/run.json").read_text())
    assert result["counts"]["failed"] == 1
    assert result["scores"]["accuracy"]["count"] == 2
    assert constructor.return_value.__exit__.call_count == 3
    assert Path("failed/report.html").is_file()
