"""Dataclass hydration, strict fields, inherited types, and snapshot isolation."""

import dataclasses
import json
import sys
import types
from dataclasses import InitVar, dataclass, field
from typing import cast

import pytest

from mic.schema import Schema, SchemaError, schema

from ._fixtures import Message, Request, Role


def test_nested_frozen_slotted_dataclasses_round_trip_as_json() -> None:
    adapter = schema(Request)
    raw = {
        "messages": [{"role": "user", "text": "hello"}],
        "by_name": {"reply": {"role": "assistant", "text": "world", "labels": ["fixed"]}},
    }
    value = adapter.validate(raw)
    assert isinstance(adapter, Schema)
    assert isinstance(value, Request)
    assert isinstance(value.messages[0], Message)
    assert value.messages[0].role is Role.USER
    assert value.by_name["reply"].labels == ["fixed"]
    dumped = adapter.dump(value)
    assert dumped == {
        "messages": [{"role": "user", "text": "hello", "labels": []}],
        "by_name": {"reply": {"role": "assistant", "text": "world", "labels": ["fixed"]}},
        "version": 1,
        "expected": None,
    }
    assert adapter.validate(json.loads(json.dumps(dumped))) == value
    with pytest.raises(dataclasses.FrozenInstanceError):
        value.messages[0].text = "illegal"


def test_defaults_are_fresh_and_native_instances_are_deeply_copied() -> None:
    adapter = schema(Request)
    raw = {"messages": [{"role": "user", "text": "hello"}], "by_name": {}}
    first, second = adapter.validate(raw), adapter.validate(raw)
    first.messages[0].labels.append("changed")
    assert second.messages[0].labels == []
    copied = adapter.validate(first)
    assert copied == first and copied is not first
    assert copied.messages is not first.messages
    assert copied.messages[0] is not first.messages[0]
    assert copied.messages[0].labels is not first.messages[0].labels
    first.messages[0].labels.append("later")
    assert copied.messages[0].labels == ["changed"]


def test_native_mutation_cannot_skip_revalidation_or_serialization_checks() -> None:
    value = Request([Message(Role.USER, "hello")], {})
    cast(list[object], value.messages[0].labels).append(123)
    for operation in (schema(Request).validate, schema(Request).dump):
        with pytest.raises(SchemaError, match=r"\$\.messages\[0\]\.labels\[0\].*expected str"):
            operation(value)


def test_post_init_runs_once_for_mapping_and_never_again_for_native_copy() -> None:
    calls: list[int] = []

    @dataclass
    class Incremented:
        value: int = 1

        def __post_init__(self) -> None:
            calls.append(self.value)
            self.value += 1

    adapter = schema(Incremented)
    value = adapter.validate({})
    assert value.value == 2
    assert calls == [1]
    copied = adapter.validate(value)
    assert copied.value == 2 and copied is not value
    assert adapter.dump(copied) == {"value": 2}
    assert calls == [1]


def test_invalid_defaults_and_post_init_changes_get_field_paths() -> None:
    @dataclass
    class InvalidDefault:
        numbers: list[int] = field(default_factory=lambda: ["bad"])

    with pytest.raises(SchemaError, match=r"\$\.numbers\[0\]"):
        schema(InvalidDefault).validate({})

    @dataclass
    class InvalidPostInit:
        score: float = 1

        def __post_init__(self) -> None:
            self.score = float("nan")

    with pytest.raises(SchemaError, match=r"\$\.score.*nonfinite"):
        schema(InvalidPostInit).validate({})


@pytest.mark.parametrize(
    "raw,path,message",
    [
        ({"messages": [], "by_name": {}, "extra": 1}, "$.extra", "unknown field"),
        ({"messages": []}, "$.by_name", "missing required"),
        (
            {"messages": [{"role": "user", "text": 1}], "by_name": {}},
            "$.messages[0].text",
            "expected str",
        ),
        ({"messages": [], "by_name": {1: {}}}, "$.by_name[key]", "keys must be strings"),
        ({"messages": [], "by_name": {}, "version": True}, "$.version", "expected one of"),
        (
            {"messages": [], "by_name": {}, "expected": {"role": "user", "text": 1}},
            "$.expected.text",
            "expected str",
        ),
    ],
)
def test_invalid_structures_report_the_precise_field(raw, path, message) -> None:
    with pytest.raises(SchemaError) as caught:
        schema(Request).validate(raw)
    assert caught.value.path == path
    assert message in str(caught.value)


def test_inherited_forward_refs_use_their_defining_module() -> None:
    module = types.ModuleType("mic_schema_test_base")
    sys.modules[module.__name__] = module
    try:
        exec(
            "from dataclasses import dataclass\n"
            "@dataclass\nclass Detail:\n    value: int\n"
            "@dataclass\nclass Base:\n    detail: 'Detail'\n",
            vars(module),
        )

        @dataclass
        class Child(module.Base):
            name: str = "child"

        value = schema(Child).validate({"detail": {"value": 2}})
        assert value.detail.value == 2
        assert schema(Child).dump(value) == {"detail": {"value": 2}, "name": "child"}
    finally:
        del sys.modules[module.__name__]


def test_unsupported_dataclass_state_is_explicit() -> None:
    @dataclass
    class Derived:
        value: int = field(init=False, default=1)

    @dataclass
    class InitializationOnly:
        value: InitVar[int]

    for annotation, message in ((Derived, "init=False"), (InitializationOnly, "InitVar")):
        with pytest.raises(SchemaError, match=message):
            schema(annotation)

    @dataclass
    class ExtraState:
        value: int

        def __post_init__(self) -> None:
            self.cached = self.value

    with pytest.raises(SchemaError, match=r"\$\.cached.*undeclared"):
        schema(ExtraState).validate({"value": 1})

    class PrivateSlot:
        __slots__ = ("__cache",)

        def initialize(self) -> None:
            self.__cache = 1

    @dataclass
    class ExtraSlotState(PrivateSlot):
        value: int

        def __post_init__(self) -> None:
            self.initialize()

    with pytest.raises(SchemaError, match=r"\$\._PrivateSlot__cache.*undeclared"):
        schema(ExtraSlotState).validate({"value": 1})


def test_inherited_keyword_only_model_hydrates_without_losing_defaults() -> None:
    @dataclass(frozen=True, slots=True, kw_only=True)
    class Identified:
        request_id: str

    @dataclass(frozen=True, slots=True, kw_only=True)
    class Conversation(Identified):
        messages: list[Message]
        attributes: dict[str, list[str]] = field(default_factory=dict)

    adapter = schema(Conversation)
    original = adapter.validate(
        {"request_id": "request-1", "messages": [{"role": "user", "text": "hello"}]}
    )
    clone = adapter.validate(original)
    clone.attributes["review"] = ["ready"]
    clone.messages[0].labels.append("reviewed")
    assert original.attributes == {}
    assert original.messages[0].labels == []
    assert adapter.dump(clone) == {
        "request_id": "request-1",
        "messages": [{"role": "user", "text": "hello", "labels": ["reviewed"]}],
        "attributes": {"review": ["ready"]},
    }
    with pytest.raises(SchemaError, match=r"\$\.request_id.*missing required"):
        adapter.validate({"messages": []})


def test_constructor_and_default_factory_failures_include_their_context() -> None:
    def unavailable_default() -> str:
        raise RuntimeError("lookup failed")

    @dataclass
    class WithFactory:
        label: str = field(default_factory=unavailable_default)

    with pytest.raises(
        SchemaError, match=r"\$\.label.*default factory failed: lookup failed"
    ) as caught:
        schema(WithFactory).validate({})
    assert isinstance(caught.value.__cause__, RuntimeError)

    @dataclass
    class WithInvariant:
        count: int

        def __post_init__(self) -> None:
            if self.count < 0:
                raise ValueError("count cannot be negative")

    with pytest.raises(SchemaError, match="constructor failed: count cannot be negative") as caught:
        schema(WithInvariant).validate({"count": -1})
    assert isinstance(caught.value.__cause__, ValueError)
