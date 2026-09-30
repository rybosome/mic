"""Typed micro-evaluations. Cloud SDKs and credentials are loaded only on demand."""

from ._runtime.materialization import ainspect_dataset, inspect_dataset
from .decorators import case_schema, dataset, eval, scorer
from .errors import ConfigurationError, DatasetError, MicError, MissingExpectedError
from .models import (
    MISSING,
    CaseSchema,
    Dataset,
    Evaluation,
    JsonObject,
    JsonValue,
    Missing,
    RawCase,
    ReadLimits,
    RunResult,
    Score,
    ScoreContext,
    Scorer,
    TaskContext,
    TaskResult,
)
from .runner import apreflight, arun, preflight, run
from .schema import Schema, SchemaError, schema

__all__ = [
    "MISSING",
    "CaseSchema",
    "ConfigurationError",
    "Dataset",
    "DatasetError",
    "Evaluation",
    "JsonObject",
    "JsonValue",
    "MicError",
    "Missing",
    "MissingExpectedError",
    "RawCase",
    "ReadLimits",
    "RunResult",
    "Schema",
    "SchemaError",
    "schema",
    "Score",
    "ScoreContext",
    "Scorer",
    "TaskContext",
    "TaskResult",
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
