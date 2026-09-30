"""Explicit cloud demonstrations using the same tasks/scorers as offline triage.

BigQuery executes a deterministic literal SELECT, billed to MIC_BIGQUERY_PROJECT.
Braintrust must point to an existing copy of examples/fixtures/triage.jsonl; its records
must include metadata.case_id matching the fixture IDs (see provider mapping notes).
No cloud request or environment lookup happens until the selected factory is invoked.
"""

import os

import mic
from examples.triage import classify_fixed, exact
from mic import JsonObject, RawCase, TaskContext
from mic.errors import DatasetError
from mic.providers.bigquery import BigQueryHandle
from mic.providers.braintrust import BraintrustHandle


@mic.dataset(name="triage.bigquery", schema=mic.case_schema(input=str, expected=str))
def bigquery_data() -> BigQueryHandle:
    return BigQueryHandle(
        billing_project=os.environ["MIC_BIGQUERY_PROJECT"],
        location=os.environ.get("MIC_BIGQUERY_LOCATION", "US"),
        maximum_bytes_billed=10_000_000,
        sql="""SELECT * FROM UNNEST([
          STRUCT('case-001' AS id, 'It crashes on startup' AS input, 'bug' AS expected),
          STRUCT('case-002' AS id, 'Please add dark mode' AS input, 'feature' AS expected),
          STRUCT('case-003' AS id, 'How do I sign in?' AS input, 'question' AS expected)
        ]) ORDER BY id""",
    )


def logical_case_id(row: object) -> RawCase:
    """Use a fixture's domain identity while retaining Braintrust physical provenance."""
    if not isinstance(row, RawCase) or not isinstance(row.metadata, dict):
        raise DatasetError("Braintrust equivalence fixture requires metadata.case_id")
    metadata = dict(row.metadata)
    case_id = metadata.pop("case_id", None)
    if not isinstance(case_id, str):
        raise DatasetError("Braintrust equivalence fixture requires string metadata.case_id")
    return RawCase(
        id=case_id,
        input=row.input,
        expected=row.expected,
        metadata=metadata or None,
        provenance=row.provenance,
    )


@mic.dataset(
    name="triage.braintrust",
    schema=mic.case_schema(input=str, expected=str),
    map_row=logical_case_id,
)
def braintrust_data() -> BraintrustHandle:
    return BraintrustHandle(
        dataset_id=os.environ["MIC_BRAINTRUST_DATASET_ID"],
        version=os.environ["MIC_BRAINTRUST_VERSION"],
    )


@mic.eval(name="triage.bigquery.fixed", dataset=bigquery_data, output=str, scorers=[exact])
def bigquery_fixed(context: TaskContext[str, JsonObject], text: str) -> str:
    return classify_fixed(text)


@mic.eval(name="triage.braintrust.fixed", dataset=braintrust_data, output=str, scorers=[exact])
def braintrust_fixed(context: TaskContext[str, JsonObject], text: str) -> str:
    return classify_fixed(text)
