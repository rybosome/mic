# Results and optional evidence

Runs return a bounded `RunResult`; they do not retain every case or write files
unless requested. Record and inspect a run explicitly:

```console
mic run ticket_eval:classify --output .mic/tickets
mic report .mic/tickets --open
```

Recording produces `run.json` and `events.jsonl`. Report generation then embeds
the summary and recorded trial results in a standalone offline HTML file. It
does not import evaluation definitions or rerun tasks. The viewer uses no remote
scripts, fonts, or network calls; evidence is rendered as text and executable code
is restricted by a content security policy.

Reports intentionally collect data for presentation. Default limits are 10,000
trials and 64 MiB combined recorded input, adjustable with `mic report --max-cases`
and `--max-bytes` or the equivalent `write_report` keyword arguments. Exceeding
a limit fails visibly; it does not silently omit trials. Large-run analysis should
consume the event stream directly.

## Public sink contract

Built-in and external sinks use the same protocol from `mic.sinks`:

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mic.results import EvaluationOutcome, RunInfo, SinkReceipt
from mic.sinks import RunEvent, SinkSession, TrialFinished


class Progress:
    name = "progress"

    @asynccontextmanager
    async def open(self, run: RunInfo) -> AsyncIterator[SinkSession]:
        yield ProgressSession()


class ProgressSession:
    async def write(self, event: RunEvent) -> None:
        if isinstance(event, TrialFinished):
            print(event.task, event.row_index, event.trial, event.result["status"])

    async def finish(self, outcome: EvaluationOutcome) -> SinkReceipt:
        return SinkReceipt("progress", "completed")
```

Pass `sinks=[Progress()]` to `run`/`arun`, or declare the sink on the
evaluation itself (below). Construction must be passive; acquire
resources in `open` and release them in the context manager. Writes are awaited
serially and apply backpressure. Do not spawn unbounded background work or keep an
unbounded event list. For blocking operations, a sink owns and joins its worker.

Events are CaseAccepted, RecordRejected, TrialFinished, and SourceFinished.
Each sink receives independent copied data; source/score summaries are bounded.
`finish` receives the computational outcome. It cannot know every other sink's
future finalization result; the returned RunResult adds those receipts afterward.
Receipt details must be a finite JSON object. The entire normalized receipt,
including any supplied failure, is capped at 64 KiB of UTF-8 JSON.

## Declared sinks

An evaluation can declare where its results go, so `mic run` uses those sinks
with no extra flags:

```python
PROGRESS = Progress()


@mic.eval(dataset=tickets, scorers=[accuracy], sinks=[PROGRESS])
def classify(ticket: Ticket) -> Label: ...
```

A declared sink is built when its module is imported, including by `mic list`,
so it must not acquire resources before `open`. `mic list` shows declared sink
names and `mic preflight` validates them; neither opens a sink.

Delivery order is the local recorder, then explicit sinks (`sinks=` or CLI
flags), then declared sinks in evaluation order. A sink instance declared by
several evaluations in a suite is opened once; duplicates are dropped by object
identity. Two different instances with the same name fail as a configuration
error before any source is read. An instance also passed explicitly is treated
as explicit.

Explicit sinks receive every event in the run. A declared sink receives only the
evaluations that declared it:

- `open` receives a `RunInfo` whose `tasks` lists only those evaluations.
- `TrialFinished` events arrive only for those evaluations.
- `CaseAccepted`, `RecordRejected`, and `SourceFinished` arrive only for sources
  those evaluations read. A dataset shared with another evaluation is still
  delivered, because its cases are inputs to the declaring evaluation.
- `finish` receives an outcome whose summary, sources, and requirement results
  cover only those evaluations; run-wide requirements such as
  `trials.task_failed == 0` are omitted. Status, exit code, and failures describe
  the whole run, so a declared sink can observe a run that failed elsewhere.

Failure semantics match explicit sinks: a write that raises disables the sink and
stops admission for the whole run; a finish or close failure fails the run. A sink
that must never affect a run should catch its own errors. Use
`mic run --no-sinks` (or `replace(evaluation, sinks=())` in Python) to run
without declared sinks, for example offline without publishing.

## Persistence failures and cancellation

Opening failure prevents source/task execution and closes previously opened sinks.
A write failure disables that sink and stops new admission; already-admitted work
finishes, and healthy sinks still receive/finalize results. Finish/close failures
are recorded separately and make the run fail. Sinks are never retried automatically.

When `output=` is selected, the local recorder is first in delivery order. Its
event file is flushed before later sinks receive each event. This does not promise
transactional remote rollback or atomic durability across sinks.

Cancellation stops admission, accounts for admitted work, joins active threads,
closes sources, and finalizes sinks before re-raising. Unread rows are not counted
as cancelled trials. A synchronous SDK call cannot be forcibly stopped, including
under repeated cancellation. Set network deadlines; an uncooperative callback or
sink can delay shutdown indefinitely.

Final run publication occurs after sink cleanup. An atomic replacement failure
gets one bounded recovery attempt and a failed manifest receipt, not task replay.
A stale `running` marker or truncated journal is possible. The returned result
keeps known summary/receipt information, not an in-memory backup of all events.

## Sensitive evidence and sharing

Recording/export sends full values: inputs, expected answers, outputs, case/task/
score metadata, labels, provenance, and generic error classifications. Local recording
of captured output (`output.jsonl`) contains whatever application code printed. Provenance
can include filesystem paths, SQL, parameters, dataset IDs, and query/job IDs.
No credentials, raw exception messages, response bodies, or tracebacks are
intentionally captured, but secrets deliberately placed in data or metadata are
not automatically recognized or redacted.

Review data and destination settings before recording, uploading, or sharing.
Treat generated HTML as a copy of the underlying evidence, not a sanitized view.
Do not commit real evaluation artifacts or credentials.

## Optional Braintrust export

Install `mic-evals[braintrust]` and set `BRAINTRUST_API_KEY`:

```console
mic run ticket_eval:classify --braintrust-project your-project
```

Python uses `BraintrustSink(project=...)` from `mic.sinks.braintrust`, passed
through `sinks=`. Local recording remains independently opt-in. Optional CLI
settings are `--braintrust-experiment`, `--braintrust-app-url`, and
`--braintrust-org`; they require a project.

Opening validates configuration/SDK/credential presence without authenticating.
The experiment is initialized lazily on the first trial event, with `update=False`
and `set_current=False`. The SDK may create the named project if it is absent.
Each trial is validated, uploaded as a top-level span, and synchronously flushed
before the next write. A later invalid score or transport error can leave partial
remote evidence. There is no batch-wide prevalidation or automatic retry.

Braintrust scores must be in [0,1] or null. Export rejection never changes local
scores. SDK 0.39.0 strips some top-level nulls; the exact trial projection in
`metadata.mic.case` retains missing versus explicit-null values. Phase timing
is exported as explicit metrics; remote span duration measures upload work.

Mic owns an isolated BraintrustState with synchronous flush and queue-drop
disabled, because the pinned SDK's default background mode can swallow failures.
The version-sensitive boundary is covered with local installed-SDK and fake
transport tests. It does not mutate the process's default experiment/logger.
Successful flush is the observable export signal, not a promise about later
server availability. No authenticated live export was performed for this redesign.

Logging failures remain primary if span cleanup also fails; cleanup notes contain
only the exception type. An uncertain failed write is not flushed or retried as
part of recovery.

SDK calls are serialized on an owned worker. Cancellation joins that worker.
A failed or cancelled upload can already have reached the server; Mic cannot
undo it. Subsequent finalization never re-executes tasks or retries uncertain writes.

## Verification

Tests exercise public sink lifecycle, mutation isolation, cancellation, filesystem
faults, malformed evidence, escaping/CSP, report limits, browser interactions, and
local SDK contracts. See [verification](verification.md) for commands and the
separate opt-in live-provider procedure.
