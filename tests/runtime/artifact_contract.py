"""Offline contract assertions; deliberately independent of runtime validation."""

import json
from functools import cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

SCHEMAS = Path(__file__).resolve().parents[2] / "docs/artifact-schemas"


@cache
def validators():
    documents = [json.loads(path.read_text(encoding="utf-8")) for path in SCHEMAS.glob("*.json")]
    # Registry has no retrieval callback: unresolved references fail, never fetch.
    registry = Registry().with_resources(
        (document["$id"], Resource.from_contents(document)) for document in documents
    )
    result = {}
    for document in documents:
        Draft202012Validator.check_schema(document)
        name = document["$id"].rsplit("/", 1)[1].removesuffix(".schema.json")
        result[name] = Draft202012Validator(
            document, registry=registry, format_checker=FormatChecker()
        )
    return result


def assert_artifact(name, value):
    # JSON itself excludes nonfinite numbers, even in arbitrary extension data.
    json.dumps(value, allow_nan=False)
    document, _, definition = name.partition("#")
    validator = validators()[document]
    if definition:
        validator = validator.evolve(schema={"$ref": validator.schema["$id"] + "#" + definition})
    errors = list(validator.iter_errors(value))
    assert not errors, "\n".join(f"{error.json_path}: {error.message}" for error in errors)


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def assert_directory(path):
    manifest = json.loads((path / "run.json").read_text(encoding="utf-8"))
    rows = read_jsonl(path / "dataset.jsonl")
    cases = read_jsonl(path / "cases.jsonl")
    assert_artifact("run-v2", manifest)
    for row in rows:
        assert_artifact("dataset-row-v2", row)
    for case in cases:
        assert_artifact("case-record-v2", case)
    return manifest, rows, cases
