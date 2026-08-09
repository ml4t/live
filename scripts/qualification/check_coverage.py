"""Enforce stable-candidate branch-aware coverage thresholds."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

OVERALL_MINIMUM = 85.0
MODULE_MINIMUMS = {
    "src/ml4t/live/engine.py": 90.0,
    "src/ml4t/live/lifecycle.py": 90.0,
    "src/ml4t/live/orders.py": 90.0,
    "src/ml4t/live/persistence.py": 90.0,
    "src/ml4t/live/runtime.py": 90.0,
    "src/ml4t/live/safety.py": 90.0,
    "src/ml4t/live/brokers/alpaca.py": 85.0,
    "src/ml4t/live/brokers/ib.py": 85.0,
}


def coverage_failures(report: dict[str, Any]) -> list[str]:
    """Return every unmet aggregate or module threshold."""
    failures = []
    overall = float(report["totals"]["percent_covered"])
    if overall < OVERALL_MINIMUM:
        failures.append(f"overall: {overall:.2f}% < {OVERALL_MINIMUM:.2f}%")
    files = report["files"]
    for path, minimum in MODULE_MINIMUMS.items():
        if path not in files:
            failures.append(f"{path}: missing from coverage report")
            continue
        actual = float(files[path]["summary"]["percent_covered"])
        if actual < minimum:
            failures.append(f"{path}: {actual:.2f}% < {minimum:.2f}%")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    failures = coverage_failures(report)
    if failures:
        print("Stable coverage policy failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("Stable coverage policy passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
