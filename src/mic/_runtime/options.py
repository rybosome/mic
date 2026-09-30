"""Resolve invocation options and validate definitions before task execution."""

import math
import os
from dataclasses import dataclass

from ..errors import ConfigurationError, DatasetError
from ..models import Evaluation, JsonObject, Missing
from .materialization import DatasetSnapshot
from .validation import nonempty, positive_integer


@dataclass(frozen=True)
class Options:
    trials: int
    concurrency: int
    model_preset: str | None
    timeout: float | None
    max_executions: int

    def as_json(self) -> JsonObject:
        return {
            "trials": self.trials,
            "concurrency": self.concurrency,
            "model_preset": self.model_preset,
            "timeout": self.timeout,
            "max_executions": self.max_executions,
        }


def resolve_options[I, O, E, M](
    spec: Evaluation[I, O, E, M],
    *,
    trials: int | None,
    concurrency: int | None,
    model_preset: str | None,
    timeout: float | None,
    max_executions: int,
) -> Options:
    nonempty("evaluation name", spec.name)
    for name, fn in (("task", spec.function), ("dataset factory", spec.dataset.factory)):
        if not callable(fn):
            raise ConfigurationError(f"{name} must be callable")
    metric_names: set[str] = set()
    scorer_names: set[str] = set()
    for scorer in spec.scorers:
        if not callable(scorer.function):
            raise ConfigurationError(f"scorer {scorer.name!r} must be callable")
        scorer_name = nonempty("scorer name", scorer.name)
        if scorer_name in scorer_names:
            raise ConfigurationError(f"Duplicate scorer name {scorer_name!r}")
        scorer_names.add(scorer_name)
        if not scorer.metrics:
            raise ConfigurationError(f"Scorer {scorer.name!r} must declare a metric")
        for metric in scorer.metrics:
            nonempty("metric name", metric)
            if metric in metric_names:
                raise ConfigurationError(f"Duplicate metric name {metric!r}")
            metric_names.add(metric)
    if timeout is not None and (
        isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0
    ):
        raise ConfigurationError("timeout must be a finite positive number")
    chosen_model = model_preset if model_preset is not None else spec.model_preset
    if chosen_model is None:
        chosen_model = os.environ.get("MIC_MODEL_PRESET")
    chosen_model = chosen_model.strip() if chosen_model and chosen_model.strip() else None
    return Options(
        positive_integer("trials", spec.trials if trials is None else trials),
        positive_integer("concurrency", spec.concurrency if concurrency is None else concurrency),
        chosen_model,
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
