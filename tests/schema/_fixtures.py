"""Ordinary domain types shared by native schema contract tests."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, NotRequired, TypedDict


class Role(Enum):
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    text: str
    labels: list[str] = field(default_factory=list)


@dataclass
class Request:
    messages: list[Message]
    by_name: Mapping[str, Message]
    version: Literal[1, 2] = 1
    expected: Message | None = None


@dataclass
class Tree:
    name: str
    children: list["Tree"] = field(default_factory=list)


class Options(TypedDict):
    name: str
    weights: NotRequired[list[float]]


type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
