# Designing evaluations

Start from the behavior the application should satisfy. Choose representative
cases, boundary cases, and known failure cases with explicit expected outcomes.
A handful of demo cases proves wiring, not production quality. Repeated trials
measure variation on the same cases; they do not add distinct coverage.

## Minimal authoring pattern

This offline example is a wiring check. Replace its task with the application
call and its cases with meaningful examples for the user's objective.
Save as `length_eval.py`:

```python
from dataclasses import dataclass

import mic


@dataclass
class Text:
    body: str


@dataclass
class Length:
    value: int


@mic.dataset(input=Text, expected=Length)
def cases() -> list[tuple[Text, Length]]:
    return [(Text(""), Length(0)), (Text("hello"), Length(5))]


@mic.scorer()
def exact(ctx: mic.ScoreContext[Text, Length]) -> float:
    return float(ctx.output.value == ctx.require_expected().value)


@mic.eval(dataset=cases, scorers=[exact])
def measure(text: Text) -> Length:
    return Length(len(text.body))
```

Run `mic run length_eval:measure`. Dataclass schemas validate inputs and outputs;
optional Pydantic support is available for applications already using it.
Factories must return fresh sources for each read, not a previously consumed
iterator. Use streaming sources for larger datasets rather than materializing them.

## Tasks and ownership

Tasks accept `(input)` or `(ctx: mic.TaskContext, input)` and return a value
matching the output annotation. Sync and async tasks/scorers can be mixed.
Keep expected answers in scorers, not in the application prompt. Task context
provides case/trial identity and metadata, not the reference answer.

Create clients during task execution or within an explicitly owned lifecycle;
close them on failure as well as success. Configure SDK timeouts and retry policy
alongside Mic's cooperative deadline. Mic itself does not retry tasks or scorers.
Keep module import and dataset descriptions passive so discovery is useful.

## Metrics and requirements

Each scorer produces one named metric. Return a finite number, a `mic.Score`
with supporting JSON metadata, or `None` when the metric does not apply.
Scores need not be between 0 and 1; name the metric and define its direction.

- A wrong answer is usually a low score, not an exception.
- Return `None` for nonapplicability, not to hide a wrong answer or evaluation failure.
- Use separate scorers for separate questions, such as accuracy and bug recall.
  Include supporting evidence in metadata when it makes judgments auditable.
- `@mic.scorer()` requires expected values by default. For reference-free metrics,
  use `@mic.scorer(requires_expected=False)`; for unlabeled datasets also choose
  `expected_policy="optional"` where appropriate. Missing and explicit null differ.
- Assess class coverage and metric counts. A high conditional mean from one
  observation should not stand in for adequate coverage.

For the offline example, this gate requires perfect scores and two observations:

```sh
mic run length_eval:measure \
  --require 'tasks["measure"].scores["exact"].mean==1' \
  --require 'tasks["measure"].scores["exact"].count>=2'
```

For real evaluations choose thresholds from the user's quality requirements,
not from whichever values make the current run pass. Use a stable evaluation set
when comparing implementations; report changes to the data, model, scorer, or
execution settings. Avoid tuning on the only set used to claim quality.

For callback signatures, metadata, custom sources, and suites, consult the
[API contracts](https://github.com/rybosome/mic/blob/main/docs/api.md).
For running and iterating, read the [CLI reference](cli.md).
