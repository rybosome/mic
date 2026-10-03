import pytest

import mic
from mic._runtime.aggregation import Aggregator, TrialObservation
from mic._runtime.requirements import parse_requirements


@pytest.fixture
def summary() -> mic.EvaluationSummary:
    aggregate = Aggregator({"classify": ["accuracy", "skip"], "empty": ["accuracy"]})
    for value in [0, 1, 1, 1]:
        aggregate.admit("classify")
        aggregate.observe(
            "classify", TrialObservation("completed", {"accuracy": value, "skip": None}, 10, 5, 15)
        )
    return aggregate.snapshot()


def parse(expression: str):
    return parse_requirements(
        [expression], {"classify": ["accuracy", "skip"], "empty": ["accuracy"]}
    )[0]


@pytest.mark.parametrize(
    ("expression", "actual", "passed"),
    [
        ('tasks["classify"].scores["accuracy"].mean >= 0.7', 0.75, True),
        ('tasks["classify"].scores["accuracy"].min >= 0.7', 0, False),
        ('tasks["classify"].scores["accuracy"].max == 1', 1, True),
        ('tasks["classify"].scores["accuracy"].count > 3', 4, True),
        ('tasks["classify"].trials.task_ms.mean <= 2e3', 10, True),
        ('tasks["classify"].trials.scoring_skipped == 0', 4, False),
        ("trials.scoring_ms.max <= 10000", 5, True),
        ("trials.total_ms.count != 0", 4, True),
        ("trials.task_failed < +1", 0, True),
        ("trials.cancelled > -1", 0, True),
        ('tasks["empty"].scores["accuracy"].mean != 0', None, False),
        ('tasks["empty"].scores["accuracy"].count == 0', 0, True),
    ],
)
def test_requirements(summary, expression, actual, passed) -> None:
    outcome = parse(expression).evaluate(summary)
    assert outcome == mic.RequirementResult(
        expression, actual, passed, "No numeric observations" if actual is None else None
    )


@pytest.mark.parametrize(
    "expression",
    [
        "accuracy>=0.7",
        "scores['accuracy'].mean > 0",
        "latency.mean < 1",
        "trials.p95 < 1",
        "trials.task_ms.p95 < 1",
        'tasks["absent"].trials.planned > 0',
        'tasks["classify"].scores["absent"].mean > 0',
        'tasks["classify"].scores["accuracy"] > 0',
        "tasks.classify.trials.planned > 0",
        "tasks['classify'].scores.accuracy.mean > 0",
        'tasks["classify"].trials["planned"] > 0',
        "tasks[1].trials.planned > 0",
        "tasks > 0",
        "trials.task_failed == True",
        "trials.task_failed == None",
        "trials.task_failed < 1e999",
        "trials.task_failed < --1",
        "trials.task_failed < float('inf')",
        "trials.task_failed is 0",
        "trials.task_failed in [0]",
        "trials.task_failed >= 0 < 1",
        "trials.task_failed + 1 < 2",
        "trials.task_failed == 0 or True",
        "__import__('os').system('false') == 0",
        "trials.__dict__ == 0",
        "trials.task_ms.__class__ == 0",
        "(lambda: 1)() > 0",
        "trials.task_failed <=",
        "x" * 4097,
        "trials.planned < " + "9" * 400,
    ],
)
def test_invalid_requirements_rejected_without_execution(expression) -> None:
    with pytest.raises(mic.ConfigurationError, match="Invalid requirement"):
        parse(expression)


def test_literal_names_can_contain_punctuation(summary) -> None:
    renamed = mic.EvaluationSummary({'task / "quoted"': summary.tasks["classify"]}, summary.trials)
    expressions = ['tasks[\'task / "quoted"\'].scores["accuracy"].mean > .7']
    parsed = parse_requirements(expressions, {'task / "quoted"': ["accuracy"]})
    assert parsed[0].evaluate(renamed).passed
    assert parse_requirements([], {}) == ()


def test_names_can_match_namespace_names(summary) -> None:
    renamed = mic.EvaluationSummary({"scores": summary.tasks["classify"]}, summary.trials)
    parsed = parse_requirements(['tasks["scores"].trials.completed == 4'], {"scores": []})
    assert parsed[0].evaluate(renamed).passed
