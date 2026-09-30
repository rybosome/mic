"""The offline human acceptance demo: baseline 2/3, fixed 3/3."""

from pathlib import Path

import mic
from mic import JsonValue, Score, ScoreContext, TaskContext
from mic.providers.files import FileHandle

type Meta = dict[str, JsonValue]


@mic.dataset(name="triage", schema=mic.case_schema(input=str, expected=str, metadata=Meta))
def triage_data() -> FileHandle:
    return FileHandle(Path(__file__).parent / "fixtures" / "triage.jsonl")


@mic.scorer(name="exact", requires_expected=True)
def exact(context: ScoreContext[str, str, str, Meta]) -> Score:
    return Score("exact", float(context.output == context.require_expected()))


@mic.eval(
    name="triage.baseline",
    dataset=triage_data,
    output=str,
    scorers=[exact],
    trials=1,
    concurrency=4,
)
def baseline(context: TaskContext[str, Meta], text: str) -> str:
    text = text.lower()
    if "bug" in text:
        return "bug"
    return "feature" if "add" in text else "question"


@mic.eval(
    name="triage.fixed",
    dataset=triage_data,
    output=str,
    scorers=[exact],
    trials=1,
    concurrency=4,
)
def fixed(context: TaskContext[str, Meta], text: str) -> str:
    return classify_fixed(text)


def classify_fixed(text: str) -> str:
    text = text.lower()
    if "bug" in text or "crashes" in text:
        return "bug"
    return "feature" if "add" in text else "question"
