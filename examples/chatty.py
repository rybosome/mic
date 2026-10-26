"""A task and scorer that print, to show how `mic run` handles application output.

`mic run examples.chatty:chatty` hides it, `--show-output` shows it with the progress
on stderr, and `--output DIR` records it to `DIR/output.jsonl`.
"""

import sys

import mic
from examples.triage import Meta, baseline, triage_data
from mic import ScoreContext, TaskContext


@mic.scorer(name="exact", requires_expected=True)
def explained(context: ScoreContext[str, str, str, Meta]) -> float:
    expected = context.require_expected()
    print(f"expected {expected!r}, got {context.output!r}")
    return float(context.output == expected)


@mic.eval(
    name="chatty", dataset=triage_data, output=str, scorers=[explained], trials=2, concurrency=4
)
def chatty(context: TaskContext[Meta], text: str) -> str:
    print(f"classifying {text!r}")
    label = baseline.function(context, text)
    print(f"chose {label}", file=sys.stderr)
    return label
