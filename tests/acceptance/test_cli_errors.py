"""Configuration failures through the actual CLI dispatcher and discovery layer."""

import json
import sys
from types import ModuleType

import pytest

import mic
import mic.cli as cli
from mic._runtime.discovery import list_definitions, resolve_definition
from mic.errors import ConfigurationError


@pytest.mark.parametrize(
    "selector", ["examples.triage", ":fixed", "examples.triage:_fixed", "a:b:c", "a:bad-name"]
)
def test_discovery_requires_an_exact_public_symbol(selector: str) -> None:
    with pytest.raises(ConfigurationError, match="module:symbol"):
        resolve_definition(selector)


def test_discovery_does_not_call_factories_or_include_private_descriptors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @mic.dataset(name="fixture", schema=mic.case_schema(input=str, expected=str))
    def data() -> object:
        raise AssertionError("listing must not invoke factory")

    module = ModuleType("_discovery_fixture")
    module.data = data
    module._private = data
    monkeypatch.setitem(sys.modules, module.__name__, module)
    definitions = list_definitions(module.__name__)
    assert len(definitions) == 1
    assert definitions[0].selector == f"{module.__name__}:data"
    assert resolve_definition(f"{module.__name__}:data") is data


def test_import_failure_is_a_configuration_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["list", "_nonexistent_mic_example"]) == 2
    assert "Cannot import" in capsys.readouterr().err


def test_in_process_inspect_dispatches_real_dataset(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["inspect", "examples.triage:triage_data", "--limit", "1"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(result["rows"]) == 1
