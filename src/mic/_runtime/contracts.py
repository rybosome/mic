"""Internal normalized values shared by stream reading and execution."""

from dataclasses import dataclass, field

from ..models import JsonObject, JsonValue, Missing


@dataclass(frozen=True)
class Case[I, E, M]:
    id: str
    input: I
    expected: E | Missing
    metadata: M | None
    provenance: JsonObject = field(default_factory=dict[str, JsonValue])
