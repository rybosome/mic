"""Typed micro-evaluations. Cloud SDKs and credentials are loaded only on demand."""

from ._runtime.datasets import ainspect_dataset, inspect_dataset
from .decorators import case_schema, dataset, eval, scorer
from .errors import ConfigurationError, DatasetError, MicError, MissingExpectedError
from .models import (
    MISSING,
    CaseSchema,
    Dataset,
    Evaluation,
    EvaluationDefinition,
    JsonObject,
    JsonValue,
    Missing,
    RawCase,
    ReadLimits,
    Score,
    ScoreContext,
    Scorer,
    TaskContext,
    TaskResult,
)
from .results import RunResult
from .runner import apreflight, arun, preflight, run
from .schema import Schema, SchemaError, schema
from .sources import DatasetSource, ReadContext, RecordError
from .summaries import EvaluationSummary, RequirementResult, Statistics, TaskSummary, TrialSummary

__all__ = [
    "MISSING",
    "CaseSchema",
    "ConfigurationError",
    "Dataset",
    "DatasetError",
    "DatasetSource",
    "Evaluation",
    "EvaluationDefinition",
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
    "schema",
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
    "scorer",
]
