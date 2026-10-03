"""The normal authoring facade stays smaller than advanced extension modules."""

import mic


def test_top_level_api_is_intentional() -> None:
    assert set(mic.__all__) == {
        "MISSING",
        "CaseSchema",
        "ConfigurationError",
        "Dataset",
        "DatasetError",
        "Evaluation",
        "EvaluationSummary",
        "JsonObject",
        "JsonValue",
        "MicError",
        "Missing",
        "MissingExpectedError",
        "RawCase",
        "ReadLimits",
        "RequirementResult",
        "RunResult",
        "Schema",
        "SchemaError",
        "Score",
        "ScoreContext",
        "Scorer",
        "Statistics",
        "TaskContext",
        "TaskResult",
        "TaskSummary",
        "TrialSummary",
        "ainspect_dataset",
        "apreflight",
        "arun",
        "case_schema",
        "dataset",
        "eval",
        "inspect_dataset",
        "preflight",
        "run",
        "schema",
        "scorer",
    }
    for removed in (
        "Case",
        "DatasetHandle",
        "DatasetLoader",
        "DatasetRead",
        "EvalSpec",
        "Reporter",
        "Resolver",
        "default_resolver",
        "envelope",
        "legacy_json_columns",
    ):
        assert not hasattr(mic, removed)


def test_extension_contracts_have_explicit_modules() -> None:
    from mic.providers import DatasetLoader, DatasetRead, Resolver
    from mic.reporters import Reporter

    assert DatasetLoader is not None
    assert DatasetRead is not None
    assert Resolver is not None
    assert Reporter is not None
