"""Validate heterogeneous selections before effects; share only identical datasets."""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import cast

from ..errors import ConfigurationError
from ..models import Dataset, Evaluation, EvaluationDefinition, JsonObject
from .datasets import describe_dataset
from .options import Options, resolve_options
from .provenance import code_provenance
from .validation import describe

type RunSelection = EvaluationDefinition | Sequence[EvaluationDefinition]
type ErasedEvaluation = Evaluation[object, object, object, object]


@dataclass(frozen=True)
class BoundTask:
    spec: ErasedEvaluation
    options: Options
    config: JsonObject


@dataclass(frozen=True)
class SourceGroup:
    dataset: Dataset[object, object, object]
    tasks: tuple[BoundTask, ...]


@dataclass(frozen=True)
class Plan:
    tasks: tuple[BoundTask, ...]
    groups: tuple[SourceGroup, ...]
    concurrency: int


def configuration[I, O, E, M](spec: Evaluation[I, O, E, M], options: Options) -> JsonObject:
    return {
        "dataset": describe_dataset(spec.dataset),
        "output": describe(spec.output, "output"),
        "options": options.as_json(),
        "scores": [scorer.name for scorer in spec.scorers],
        "provenance": code_provenance(spec.function),
    }


def plan(
    selection: RunSelection,
    *,
    trials: int | None,
    concurrency: int | None,
    timeout: float | None,
    max_executions: int,
) -> Plan:
    candidates = (selection,) if isinstance(selection, EvaluationDefinition) else tuple(selection)
    specs: list[ErasedEvaluation] = []
    names: set[str] = set()
    identities: set[int] = set()
    for item in candidates:
        if not isinstance(item, Evaluation):
            raise ConfigurationError("Run selections must contain evaluation definitions")
        # Type erasure is confined to selection. Typed decorators have already
        # bound compatible callbacks/schemas; dispatch never mixes their fields.
        spec = cast(ErasedEvaluation, item)
        if id(spec) in identities:
            continue
        if spec.name in names:
            raise ConfigurationError(f"Duplicate evaluation name {spec.name!r}")
        names.add(spec.name)
        identities.add(id(spec))
        specs.append(spec)
    if not specs:
        raise ConfigurationError("Select at least one evaluation")
    options = [
        resolve_options(
            spec,
            trials=trials,
            concurrency=concurrency,
            timeout=timeout,
            max_executions=max_executions,
        )
        for spec in specs
    ]
    capacity = max(option.concurrency for option in options)
    options = [replace(option, concurrency=capacity) for option in options]
    tasks = tuple(
        BoundTask(spec, option, configuration(spec, option))
        for spec, option in zip(specs, options, strict=True)
    )
    grouped: dict[int, list[BoundTask]] = {}
    for task in tasks:
        grouped.setdefault(id(task.spec.dataset), []).append(task)
    return Plan(
        tasks,
        tuple(SourceGroup(group[0].spec.dataset, tuple(group)) for group in grouped.values()),
        capacity,
    )
