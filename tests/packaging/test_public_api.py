"""The normal authoring facade stays smaller than advanced extension modules."""

import mic


def test_top_level_api_is_intentional() -> None:
    assert set(mic.__all__) == {
        "MISSING",
        "CaseSchema",
        "ConfigurationError",
        "Dataset",
        "DatasetError",
        "DatasetSource",
        "Evaluation",
        "EvaluationSummary",
        "JsonObject",
        "JsonValue",
        "MicError",
        "Missing",
        "MissingExpectedError",
        "RawCase",
        "ReadLimits",
        "ReadContext",
        "RecordError",
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
    from mic.sinks import ResultSink
    from mic.sources import DatasetSource, ReadContext, RecordError

    assert DatasetSource is mic.DatasetSource
    assert ReadContext is mic.ReadContext
    assert RecordError is mic.RecordError
    assert ResultSink is not None
