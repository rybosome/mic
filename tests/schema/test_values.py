"""Native value types, recursive data, schema documents, and isolated imports."""

import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pytest

import mic
from mic.schema import SchemaError, schema

from ._fixtures import JsonValue, Options, Request, Role, Tree


@pytest.mark.parametrize(
    "annotation,value",
    [
        (int, True),
        (int, "1"),
        (float, "1.5"),
        (bool, 1),
        (str, 123),
        (list[int], (1, 2)),
        (float, float("inf")),
    ],
)
def test_primitives_are_strict(annotation, value) -> None:
    with pytest.raises(SchemaError):
        schema(annotation).validate(value)


def test_tuple_map_nullable_literal_and_enum_schemas() -> None:
    assert schema(tuple[int, str]).validate([2, "two"]) == (2, "two")
    assert schema(tuple[int, ...]).dump((1, 2, 3)) == [1, 2, 3]
    assert schema(tuple[()]).validate([]) == ()
    with pytest.raises(SchemaError, match="length 2"):
        schema(tuple[int, str]).validate([2])
    assert schema(str | None).validate(None) is None
    assert schema(Mapping[str, list[int]]).validate({"a": [1]}) == {"a": [1]}
    assert schema(float).validate(2) == 2.0
    assert schema(Role).validate("assistant") is Role.ASSISTANT
    assert schema(Literal[Role.USER]).dump(Role.USER) == "user"
    assert schema(Sequence[int]).validate(range(3)) == [0, 1, 2]
    with pytest.raises(SchemaError):
        schema(Sequence[str]).validate("text is not an array")


def test_typed_dict_required_optional_and_unknown_fields() -> None:
    adapter = schema(Options)
    assert adapter.validate({"name": "small"}) == {"name": "small"}
    assert adapter.validate({"name": "weighted", "weights": [1, 0.5]}) == {
        "name": "weighted",
        "weights": [1.0, 0.5],
    }
    with pytest.raises(SchemaError, match=r"\$\.name.*missing required"):
        adapter.validate({"weights": []})
    with pytest.raises(SchemaError, match=r"\$\.extra.*unknown"):
        adapter.validate({"name": "small", "extra": True})


def test_recursive_dataclasses_and_json_aliases() -> None:
    tree_schema = schema(Tree)
    tree = tree_schema.validate({"name": "root", "children": [{"name": "leaf"}]})
    assert tree.children[0] == Tree("leaf")
    assert tree_schema.dump(tree) == {
        "name": "root",
        "children": [{"name": "leaf", "children": []}],
    }
    json_schema = schema(dict[str, JsonValue])
    raw = {"items": [None, True, 1, 1.5, "hello", {"nested": [False]}]}
    assert json_schema.validate(raw) == raw
    assert json_schema.dump(raw) == raw
    assert schema(Any).validate(raw) == raw
    assert schema(object).validate(raw) == raw


@pytest.mark.parametrize("kind", ["dataclass", "list", "dict", "alias"])
def test_cycles_have_field_paths_but_shared_values_are_not_cycles(kind: str) -> None:
    if kind == "dataclass":
        value = Tree("root")
        value.children.append(value)
        adapter, path = schema(Tree), "$.children[0]"
    elif kind in ("list", "alias"):
        value = []
        value.append(value)
        adapter, path = schema(object if kind == "list" else JsonValue), "$[0]"
    else:
        value = {}
        value["loop"] = value
        adapter, path = schema(object), "$.loop"
    with pytest.raises(SchemaError) as caught:
        adapter.validate(value)
    assert caught.value.path == path
    assert "cyclic" in str(caught.value)
    shared = {"shared": [1]}
    copied = schema(object).validate([shared, shared])
    assert copied == [shared, shared]
    assert copied[0] is not copied[1]


def test_runtime_json_schemas_resolve_all_references_without_running_defaults() -> None:
    calls: list[bool] = []

    def default() -> list[int]:
        calls.append(True)
        return []

    @dataclass
    class WithDefault:
        numbers: list[int] = field(default_factory=default)

    assert schema(WithDefault).json_schema()
    assert calls == []
    for annotation in (Request, Tree, JsonValue, Options):
        document = schema(annotation).json_schema()
        assert json.loads(json.dumps(document)) == document

        def inspect_refs(value: object) -> None:
            if isinstance(value, dict):
                if "$ref" in value:
                    name = value["$ref"].removeprefix("#/$defs/")
                    assert name in document["$defs"]
                for item in value.values():
                    inspect_refs(item)
            elif isinstance(value, list):
                for item in value:
                    inspect_refs(item)

        inspect_refs(document)
    request = schema(Request).json_schema()["$defs"]["Request"]
    assert request["required"] == ["messages", "by_name"]
    assert request["additionalProperties"] is False


@pytest.mark.parametrize("annotation", [set[int], bytes, dict[int, str], complex])
def test_unsupported_annotations_fail_at_definition_time(annotation) -> None:
    with pytest.raises(SchemaError):
        schema(annotation)


def test_nonproductive_recursive_alias_and_lax_mode_fail_explicitly() -> None:
    type InvalidAlias = InvalidAlias | int

    with pytest.raises(SchemaError, match="recursive annotation"):
        schema(InvalidAlias)
    with pytest.raises(SchemaError, match="strict=True"):
        schema(int).validate(1, strict=False)


def test_schema_has_no_third_party_runtime_dependency() -> None:
    source = Path(mic.__file__).parents[1]
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(source)!r})\n"
        "from mic.schema import schema\n"
        "from dataclasses import dataclass\n"
        "@dataclass\nclass Row:\n    values: list[int]\n"
        "assert schema(Row).validate({'values':[1]}).values == [1]\n"
        "assert 'pydantic' not in sys.modules\n"
        "assert 'typing_extensions' not in sys.modules\n"
    )
    completed = subprocess.run([sys.executable, "-S", "-c", code], text=True, capture_output=True)
    assert completed.returncode == 0, completed.stderr


def test_schema_documents_disambiguate_distinct_domain_types_with_the_same_name() -> None:
    from dataclasses import make_dataclass

    text_value = make_dataclass("Value", [("text", str)])
    numeric_value = make_dataclass("Value", [("count", int)])
    envelope = make_dataclass("Envelope", [("message", text_value), ("metric", numeric_value)])
    adapter = schema(envelope)
    document = adapter.json_schema()
    properties = document["$defs"]["Envelope"]["properties"]
    text_reference = properties["message"]["$ref"].removeprefix("#/$defs/")
    number_reference = properties["metric"]["$ref"].removeprefix("#/$defs/")
    assert text_reference != number_reference
    assert document["$defs"][text_reference]["properties"] == {"text": {"type": "string"}}
    assert document["$defs"][number_reference]["properties"] == {"count": {"type": "integer"}}
    value = adapter.validate({"message": {"text": "value"}, "metric": {"count": 2}})
    assert adapter.dump(value) == {"message": {"text": "value"}, "metric": {"count": 2}}
    with pytest.raises(SchemaError, match=r"\$\.metric\.count.*expected int"):
        adapter.validate({"message": {"text": "value"}, "metric": {"count": "2"}})


def test_nested_schema_failure_does_not_poison_a_reusable_adapter() -> None:
    adapter = schema(dict[str, list[Tree]])
    with pytest.raises(SchemaError, match=r"\$\.batch\[1\]\.children\[0\]\.name"):
        adapter.validate({"batch": [{"name": "good"}, {"name": "bad", "children": [{"name": 2}]}]})
    valid = {"batch": [{"name": "root", "children": [{"name": "leaf", "children": []}]}]}
    assert adapter.dump(adapter.validate(valid)) == valid
