"""The mic command line uses the same runner as the Python API."""

import argparse
import json
import sys
import webbrowser
from collections.abc import Sequence
from pathlib import Path

import mic
from mic._runtime.artifacts import has_artifact_failure
from mic._runtime.discovery import list_definitions, resolve_definition
from mic.errors import ConfigurationError, DatasetError, MicError
from mic.models import JsonObject
from mic.reporters.braintrust import BraintrustReporter
from mic.reporters.console import format_summary
from mic.reporters.html import write_report


def _limits(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-rows", type=int, default=10_000)
    parser.add_argument("--max-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--max-record-bytes", type=int, default=1024 * 1024)
    parser.add_argument(
        "--dataset-timeout",
        type=float,
        default=60.0,
        help="Overall dataset materialization deadline in seconds",
    )


def _execution(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--trials", type=int)
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--max-executions", type=int, default=50_000)
    _limits(parser)
    parser.add_argument("--braintrust-project", help="Opt in to exporting this run to Braintrust")
    parser.add_argument("--braintrust-experiment")
    parser.add_argument("--braintrust-app-url")
    parser.add_argument("--braintrust-org")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mic", description="Typed evaluations with local, inspectable run evidence."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List definitions without loading datasets")
    listing.add_argument("modules", nargs="+")
    listing.add_argument("--json", action="store_true")
    inspect = commands.add_parser("inspect", help="Validate and inspect a selected dataset prefix")
    inspect.add_argument("selector", help="Exact module:symbol for a dataset or evaluation")
    inspect.add_argument("--limit", type=int, help="Explicitly select only this many source rows")
    _limits(inspect)
    preflight = commands.add_parser(
        "preflight", help="Validate data and configuration; execute no tasks"
    )
    preflight.add_argument("selector")
    _execution(preflight)
    run = commands.add_parser("run", help="Run an evaluation and save local artifacts")
    run.add_argument("selector")
    _execution(run)
    run.add_argument("--output", type=Path)
    run.add_argument("--timeout", type=float, help="Cooperative per-trial timeout in seconds")
    run.add_argument("--require", action="append", default=[], metavar="METRIC>=VALUE")
    run.add_argument("--json", action="store_true", help="Print the complete final run manifest")
    report = commands.add_parser(
        "report", help="Render existing artifacts without running an evaluation"
    )
    report.add_argument("run_path", type=Path, help="Run directory or run.json")
    report.add_argument("--output", type=Path)
    report.add_argument("--open", action="store_true", help="Open the HTML in your default browser")
    return parser


def _read_limits(args: argparse.Namespace) -> mic.ReadLimits:
    return mic.ReadLimits(
        max_rows=args.max_rows,
        max_bytes=args.max_bytes,
        max_record_bytes=args.max_record_bytes,
        timeout_seconds=args.dataset_timeout,
    )


def _reporters(args: argparse.Namespace) -> list[BraintrustReporter]:
    if not args.braintrust_project:
        if any((args.braintrust_experiment, args.braintrust_app_url, args.braintrust_org)):
            raise ConfigurationError("Braintrust options require --braintrust-project")
        return []
    return [
        BraintrustReporter(
            project=args.braintrust_project,
            experiment=args.braintrust_experiment,
            app_url=args.braintrust_app_url,
            org_name=args.braintrust_org,
        )
    ]


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False))


def _execute(args: argparse.Namespace) -> int:
    if args.command == "list":
        rows = [definition for module in args.modules for definition in list_definitions(module)]
        if args.json:
            _print_json(
                [{"kind": row.kind, "name": row.name, "selector": row.selector} for row in rows]
            )
        else:
            for row in rows:
                print(f"{row.kind:8} {row.name:28} {row.selector}")
        return 0
    if args.command == "report":
        destination = write_report(args.run_path, output=args.output).resolve()
        print(destination)
        if args.open:
            webbrowser.open(destination.as_uri())
        return 0
    definition = resolve_definition(args.selector)
    if args.command == "inspect":
        dataset = definition.dataset if isinstance(definition, mic.Evaluation) else definition
        if not isinstance(dataset, mic.Dataset):
            raise ConfigurationError(f"{args.command} requires a dataset or evaluation definition")
        _print_json(mic.inspect_dataset(dataset, limit=args.limit, limits=_read_limits(args)))
        return 0
    if not isinstance(definition, mic.Evaluation):
        raise ConfigurationError(f"{args.command} requires an evaluation definition")
    reporters = _reporters(args)
    if args.command == "preflight":
        result: JsonObject = mic.preflight(
            definition,
            trials=args.trials,
            concurrency=args.concurrency,
            limits=_read_limits(args),
            max_executions=args.max_executions,
            reporters=reporters,
        )
        _print_json(result)
        return 0
    run_result = mic.run(
        definition,
        output=args.output,
        trials=args.trials,
        concurrency=args.concurrency,
        timeout=args.timeout,
        require=args.require,
        limits=_read_limits(args),
        max_executions=args.max_executions,
        reporters=reporters,
    )
    if args.json:
        _print_json(run_result.manifest)
    else:
        print(format_summary(run_result.manifest))
        if has_artifact_failure(run_result.manifest):
            print(f"Evidence     {run_result.output_dir} may be incomplete or stale")
        else:
            print(f"Report       {run_result.output_dir / 'report.html'}")
    return run_result.exit_code


def main(argv: Sequence[str] | None = None) -> int:
    """Exit 0: completed; 1: execution/gate/report; 2: configuration/data; 130: interrupt."""
    args = build_parser().parse_args(argv)
    # The console entry point runs from bin/, so explicitly enable user-selected modules
    # in the invocation directory. Importing selected Python modules executes their code.
    current = str(Path.cwd())
    if current not in sys.path:
        sys.path.insert(0, current)
    try:
        return _execute(args)
    except (ConfigurationError, DatasetError) as exc:
        print(f"mic: {exc}", file=sys.stderr)
        for note in getattr(exc, "__notes__", []):
            print(note, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("mic: interrupted", file=sys.stderr)
        return 130
    except (MicError, OSError, ValueError) as exc:
        print(f"mic: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
