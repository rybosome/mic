"""Optional Pydantic compatibility without coupling the core to its dependency."""

import subprocess
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated

import pytest

import mic.integrations.pydantic as integration
from mic.errors import ConfigurationError
from mic.integrations.pydantic import pydantic_schema

pydantic = pytest.importorskip("pydantic", reason="optional Pydantic integration not installed")
BaseModel = pydantic.BaseModel
TypeAdapter = pydantic.TypeAdapter
Field = pydantic.Field


@dataclass
class PositiveCount:
    count: Annotated[int, Field(ge=0)]


class NestedModel(BaseModel):
    item: PositiveCount
    tags: list[str]


class AliasModel(BaseModel):
    model_config = pydantic.ConfigDict(serialize_by_alias=True)
    value: int = Field(alias="external_value")


def test_explicit_schema_hydrates_models_and_nested_dataclasses() -> None:
    schema = pydantic_schema(NestedModel)
    value = schema.validate({"item": {"count": 2}, "tags": ["a"]})
    assert isinstance(value, NestedModel)
    assert isinstance(value.item, PositiveCount)
    assert schema.dump(value) == {"item": {"count": 2}, "tags": ["a"]}
    assert schema.json_schema()["type"] == "object"
    with pytest.raises(ValueError, match="greater than or equal"):
        schema.validate({"item": {"count": -1}, "tags": []})


def test_native_model_and_dataclass_cannot_bypass_constraints() -> None:
    invalid = PositiveCount(-1)
    with pytest.raises(ValueError, match="greater than or equal"):
        pydantic_schema(PositiveCount).validate(invalid)
    forged = NestedModel.model_construct(item=invalid, tags=[])
    with pytest.raises(ValueError, match="greater than or equal"):
        pydantic_schema(NestedModel).validate(forged)
    with pytest.raises(ValueError, match="Field required"):
        pydantic_schema(NestedModel).validate(NestedModel.model_construct(tags=[]))


def test_native_instances_are_rehydrated_and_do_not_alias() -> None:
    original = NestedModel(item=PositiveCount(1), tags=["first"])
    schema = pydantic_schema(NestedModel)
    first = schema.validate(original)
    original.tags.append("second")
    original.item.count = 2
    second = schema.validate(original)
    assert first.tags == ["first"] and first.item.count == 1
    assert second.tags == ["first", "second"] and second.item.count == 2
    assert first is not original and first.item is not original.item


def test_aliases_accept_external_and_canonical_keys_but_dump_canonically() -> None:
    schema = pydantic_schema(AliasModel)
    external = schema.validate({"external_value": 2})
    native = schema.validate(external)
    assert schema.dump(native) == {"value": 2}
    restored = schema.validate(schema.dump(native))
    assert restored.value == 2
    assert set(schema.json_schema()["properties"]) == {"value"}


@pytest.mark.parametrize("value", ["2", True, 2.2])
def test_strict_primitives_remain_strict(value: object) -> None:
    schema = pydantic_schema(TypeAdapter(int))
    with pytest.raises(ValueError):
        schema.validate(value)
    assert schema.validate("2", strict=False) == 2


def test_annotated_validator_runs_once_and_dump_does_not_revalidate() -> None:
    calls: list[int] = []

    def record(value: int) -> int:
        calls.append(value)
        return value * 2

    schema = pydantic_schema(TypeAdapter(Annotated[int, pydantic.AfterValidator(record)]))
    value = schema.validate(2)
    assert value == 4
    assert schema.dump(value) == 4
    assert calls == [2]


def test_union_adapter_and_explicit_null() -> None:
    schema = pydantic_schema(TypeAdapter(str | None))
    assert schema.validate(None) is None
    assert schema.dump(None) is None
    assert schema.validate("present") == "present"


def test_nonfinite_native_model_checked_before_serializers_hide_it() -> None:
    class Masked(BaseModel):
        value: float

        @pydantic.field_serializer("value")
        def mask(self, value: float) -> None:
            return None

    schema = pydantic_schema(Masked)
    value = Masked.model_construct(value=float("nan"))
    with pytest.raises(ValueError, match="nonfinite"):
        schema.validate(value)
    with pytest.raises(ValueError, match="nonfinite"):
        schema.dump(value)


def test_nonfinite_excluded_fields_and_extra_model_fields_are_rejected() -> None:
    class Extras(BaseModel):
        model_config = pydantic.ConfigDict(extra="allow")
        value: float
        excluded: float = Field(default=0, exclude=True)

    schema = pydantic_schema(Extras)
    for value in (
        Extras.model_construct(value=1, excluded=float("inf")),
        Extras.model_construct(value=1, extra=Decimal("NaN")),
    ):
        with pytest.raises(ValueError, match="nonfinite"):
            schema.dump(value)


def test_nonfinite_mapping_keys_cannot_be_hidden_by_string_serialization() -> None:
    schema = pydantic_schema(TypeAdapter(dict[float, str]))
    value = {float("nan"): "example"}
    with pytest.raises(ValueError, match="nonfinite"):
        schema.validate(value)
    with pytest.raises(ValueError, match="nonfinite"):
        schema.dump(value)


def test_validators_and_serializers_cannot_introduce_nonfinite_values() -> None:
    schema = pydantic_schema(
        TypeAdapter(Annotated[float, pydantic.AfterValidator(lambda _: float("nan"))])
    )
    with pytest.raises(ValueError, match="nonfinite"):
        schema.validate(1.0)

    class BadSerializer(BaseModel):
        value: float

        @pydantic.field_serializer("value")
        def nonfinite(self, value: float) -> float:
            return float("inf")

    serializer_schema = pydantic_schema(BadSerializer)
    with pytest.raises(ValueError, match="nonfinite"):
        serializer_schema.dump(BadSerializer(value=1))
    with pytest.raises(ValueError, match="nonfinite"):
        serializer_schema.validate(BadSerializer(value=1))


def test_missing_optional_dependency_has_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(name: str) -> object:
        assert name == "pydantic"
        raise ImportError("Pydantic unavailable")

    monkeypatch.setattr(integration, "importlib", SimpleNamespace(import_module=missing))
    with pytest.raises(ConfigurationError, match=r"mic-evals\[pydantic\]"):
        pydantic_schema(int)


def test_optional_module_import_does_not_import_pydantic() -> None:
    root = Path(__file__).resolve().parents[2]
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            """import sys
class BlockPydantic:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "pydantic" or fullname.startswith("pydantic."):
            raise AssertionError("Pydantic imported by core or optional module import")
sys.meta_path.insert(0, BlockPydantic())
import mic
from mic.integrations.pydantic import pydantic_schema
assert "pydantic" not in sys.modules
print("lazy optional import passed")
""",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    assert "lazy optional import passed" in process.stdout
