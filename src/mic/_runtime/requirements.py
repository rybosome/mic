"""A closed accessor/comparison grammar; expressions are never executed."""

import ast
import math
import operator
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from ..errors import ConfigurationError
from ..summaries import EvaluationSummary, RequirementResult, Statistics

_COUNTS = {"planned", "completed", "task_failed", "scoring_failed", "scoring_skipped", "cancelled"}
_TIMINGS = {"task_ms", "scoring_ms", "total_ms"}
_STATISTICS = {"count", "mean", "min", "max"}
_COMPARISONS: dict[type[ast.cmpop], Callable[[float, float], bool]] = {
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
}


@dataclass(frozen=True)
class Requirement:
    expression: str
    path: tuple[str, ...]
    comparison: type[ast.cmpop]
    threshold: int | float

    def evaluate(self, summary: EvaluationSummary) -> RequirementResult:
        path = self.path
        scope = summary
        task = None
        if path[0] == "tasks":
            task = summary.tasks[path[1]]
            path = path[2:]
        if path[0] == "scores":
            assert task is not None
            stats = task.scores[path[1]]
            actual = _statistic(stats, path[2])
        else:
            trials = scope.trials if task is None else task.trials
            if len(path) == 2:
                actual = cast(int, getattr(trials, path[1]))
            else:
                actual = _statistic(cast(Statistics, getattr(trials, path[1])), path[2])
        passed = actual is not None and _COMPARISONS[self.comparison](actual, self.threshold)
        return RequirementResult(
            self.expression, actual, passed, "No numeric observations" if actual is None else None
        )


def _statistic(stats: Statistics, name: str) -> int | float | None:
    return cast(int | float | None, getattr(stats, name))


def _path(node: ast.expr) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        base = _path(node.value)
        if base == ("tasks",) or (len(base) == 3 and base[2] == "scores"):
            raise ValueError("Task and score names require string subscripts")
        return (*base, node.attr)
    if isinstance(node, ast.Subscript):
        base = _path(node.value)
        if base != ("tasks",) and not (len(base) == 3 and base[2] == "scores"):
            raise ValueError("Only task and score maps accept subscripts")
        if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            return (*base, node.slice.value)
    raise ValueError("Expected a summary field or a literal task/score key")


def _bound(node: ast.expr) -> int | float:
    sign = 1
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        sign = -1 if isinstance(node.op, ast.USub) else 1
        node = node.operand
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        value = sign * cast(int | float, node.value)
        try:
            if math.isfinite(value):
                return value
        except OverflowError:
            pass
    raise ValueError("The comparison bound must be a finite numeric literal")


def _validate_path(path: tuple[str, ...], tasks: Mapping[str, Sequence[str]]) -> None:
    if path[0] == "tasks":
        if len(path) < 3 or path[1] not in tasks:
            raise ValueError("Unknown task")
        metrics = tasks[path[1]]
        path = path[2:]
        if path[0] == "scores":
            if len(path) == 3 and path[1] in metrics and path[2] in _STATISTICS:
                return
            raise ValueError("Unknown score or statistic")
    if path[0] == "trials":
        if len(path) == 2 and path[1] in _COUNTS:
            return
        if len(path) == 3 and path[1] in _TIMINGS and path[2] in _STATISTICS:
            return
    raise ValueError("Unknown summary field")


def parse_requirements(
    expressions: Sequence[str], tasks: Mapping[str, Sequence[str]]
) -> tuple[Requirement, ...]:
    parsed: list[Requirement] = []
    for expression in expressions:
        try:
            if len(expression) > 4096:
                raise ValueError("Expression exceeds 4096 characters")
            tree = ast.parse(expression.strip(), mode="eval").body
            if not isinstance(tree, ast.Compare) or len(tree.ops) != 1:
                raise ValueError("Expected one accessor comparison, e.g. trials.task_failed == 0")
            comparison = type(tree.ops[0])
            if comparison not in _COMPARISONS:
                raise ValueError("Unsupported comparison")
            path = _path(tree.left)
            _validate_path(path, tasks)
            parsed.append(Requirement(expression, path, comparison, _bound(tree.comparators[0])))
        except (SyntaxError, ValueError, RecursionError) as exc:
            raise ConfigurationError(f"Invalid requirement {expression!r}: {exc}") from None
    return tuple(parsed)
