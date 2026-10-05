---
name: mic
description: Install Mic, write and run typed evaluations, and interpret its CLI results and saved evidence. Use when working with mic-evals evaluations or artifacts.
---

# Use Mic

Mic evaluates Python application calls against typed cases and named scorers.
The distribution is `mic-evals`; the import and executable are `mic`.

## Workflow

1. Identify the application, behavior to measure, dataset, and intended scope.
   Use the application's Python environment so Mic can import its dependencies.
   For setup or import failures, read [installation](references/installation.md).
2. When writing or changing an evaluation, read
   [designing evaluations](references/designing-evals.md). Keep dataset factories
   fresh and clients inside task execution, with explicit resource cleanup.
3. Before constructing or changing execution commands, read the
   [CLI reference](references/cli.md). Choose discovery, inspection, preflight,
   or execution based on what needs checking; they have different side effects.
4. Start with a bounded run appropriate to the requested diagnostic or experiment.
   Record evidence explicitly when individual results will be needed. Credentials
   being available do not expand the user's authorized datasets, destinations, or
   spending scope. Preserve authorization already given for the task.
5. Read [interpreting results](references/interpreting-results.md) before drawing
   conclusions. Check execution, coverage, observations, requirements, and recording
   outcomes; connect failures to the next useful CLI action rather than blindly retrying.
6. Report the command, exit status, evaluated scope, metrics with observation counts,
   relevant failures, artifact paths, and limits of the conclusion. Separate a
   successful smoke test from evidence of application quality.

## Operating boundaries

- Importing an evaluation executes its module-level Python. Discovery does not call
  dataset factories or tasks, but cannot make arbitrary imported code side-effect-free.
- Inspection and preflight read datasets, including cloud sources. Preflight does
  not call tasks or scorers and does not prove live task credentials, connectivity,
  or imports inside a task will work.
- Model calls and remote sinks are explicit effects. Use the user's existing
  authorization; ask only for missing scope before introducing additional effects.
- Keep secrets out of commands, logs, fixtures, and artifacts. Evidence may contain
  sensitive inputs and outputs; generic exception messages are not general redaction.
- A rerun repeats application calls and possible charges. Prefer existing evidence
  when the question can be answered without another run.

These references describe Mic 0.2's CLI and `mic-run-v4` / `mic-event-v1` evidence.
Check the installed package version and command help when behavior differs.
For further contracts, use the project's
[documentation](https://github.com/rybosome/mic/tree/main/docs), matching its revision
to the installed version when necessary.
