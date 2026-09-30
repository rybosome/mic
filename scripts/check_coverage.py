"""Enforce separate statement and branch floors from fresh coverage.py JSON."""

import argparse
import json
from pathlib import Path

FLOORS = {"statements": 93, "branches": 84}


def evaluate(report):
    totals = report["totals"]
    measurements = {}
    for name, covered_key, total_key in (
        ("statements", "covered_lines", "num_statements"),
        ("branches", "covered_branches", "num_branches"),
    ):
        covered, total = totals[covered_key], totals[total_key]
        if type(covered) is not int or type(total) is not int or not 0 <= covered <= total:
            raise ValueError(f"Invalid {name} counts")
        if name == "statements" and total == 0:
            raise ValueError("No statements measured")
        measurements[name] = {
            "covered": covered,
            "total": total,
            "percent": 100 * covered / total if total else 100.0,
            "minimum_percent": FLOORS[name],
            # Compare counts, not rounded display percentages.
            "passed": 100 * covered >= FLOORS[name] * total,
        }
    return {"passed": all(item["passed"] for item in measurements.values()), **measurements}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = evaluate(json.loads(args.report.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result = {"passed": False, "error": f"Cannot evaluate coverage: {exc}"}
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return int(not result["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
