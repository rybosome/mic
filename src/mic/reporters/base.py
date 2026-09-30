"""Public contract for optional run reporters."""

from collections.abc import Sequence
from typing import Protocol

from ..models import JsonObject


class Reporter(Protocol):
    @property
    def name(self) -> str: ...

    async def prepare(self) -> None: ...

    async def report(self, manifest: JsonObject, cases: Sequence[JsonObject]) -> JsonObject: ...
