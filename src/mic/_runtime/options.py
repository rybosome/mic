"""Resolve invocation options and validate definitions before task execution."""

import math
from dataclasses import dataclass

from ..errors import ConfigurationError
from ..models import Evaluation, JsonObject
from .validation import nonempty, positive_integer


@dataclass(frozen=True)
class Options:
    trials: int
    concurrency: int
    timeout: float | None
    max_executions: int

    def as_json(self) -> JsonObject:
        return {
            "trials": self.trials,
            "concurrency": self.concurrency,
            "timeout": self.timeout,
            "max_executions": self.max_executions,
        }


def resolve_options[I, O, E, M](
    spec: Evaluation[I, O, E, M],
    *,
    trials: int | None,
    concurrency: int | None,
    timeout: float | None,
    max_executions: int,
) -> Options:
    nonempty("evaluation name", spec.name)
    for name, fn in (("task", spec.function), ("dataset factory", spec.dataset.factory)):
        if not callable(fn):
            raise ConfigurationError(f"{name} must be callable")
    scorer_names: set[str] = set()
    for scorer in spec.scorers:
        if not callable(scorer.function):
            raise ConfigurationError(f"scorer {scorer.name!r} must be callable")
        scorer_name = nonempty("scorer name", scorer.name)
        if scorer_name in scorer_names:
            raise ConfigurationError(f"Duplicate scorer name {scorer_name!r}")
        scorer_names.add(scorer_name)
    if timeout is not None and (
        isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0
    ):
        raise ConfigurationError("timeout must be a finite positive number")
    return Options(
        positive_integer("trials", spec.trials if trials is None else trials),
        positive_integer("concurrency", spec.concurrency if concurrency is None else concurrency),
        timeout,
        positive_integer("max_executions", max_executions),
    )
