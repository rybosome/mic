"""Resolve invocation options and validate definitions before task execution."""

import math
from dataclasses import dataclass

from ..errors import ConfigurationError, DatasetError
from ..models import Evaluation, JsonObject, Missing
from .materialization import DatasetSnapshot
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


def check_cases[I, O, E, M](
    spec: Evaluation[I, O, E, M],
    snapshot: DatasetSnapshot[I, E, M],
    options: Options,
) -> None:
    executions = len(snapshot.cases) * options.trials
    if executions > options.max_executions:
        raise ConfigurationError(
            f"{executions} executions exceed max_executions={options.max_executions}"
        )
    needed = [scorer.name for scorer in spec.scorers if scorer.requires_expected]
    if needed:
        for row in snapshot.cases:
            if isinstance(row.expected, Missing):
                raise DatasetError(
                    f"Case {row.id!r} is unlabeled, but scorers {needed!r} require expected"
                )
