"""Tests for stable branch-aware coverage policy enforcement."""

from __future__ import annotations

import runpy
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

SCRIPT = Path(__file__).parents[2] / "scripts" / "qualification" / "check_coverage.py"
NAMESPACE = runpy.run_path(str(SCRIPT))


def report(*, overall: float = 85.0, module: float = 90.0, broker: float = 85.0):
    minimums = cast(dict[str, float], NAMESPACE["MODULE_MINIMUMS"])
    return {
        "totals": {"percent_covered": overall},
        "files": {
            path: {
                "summary": {
                    "percent_covered": broker if "/brokers/" in path else module,
                }
            }
            for path in minimums
        },
    }


def test_coverage_policy_accepts_exact_thresholds() -> None:
    coverage_failures = cast(Callable[[dict[str, Any]], list[str]], NAMESPACE["coverage_failures"])

    assert coverage_failures(report()) == []


def test_coverage_policy_reports_aggregate_module_broker_and_missing_faults() -> None:
    coverage_failures = cast(Callable[[dict[str, Any]], list[str]], NAMESPACE["coverage_failures"])
    candidate = report(overall=84.99, module=89.99, broker=84.99)
    del candidate["files"]["src/ml4t/live/orders.py"]

    failures = coverage_failures(candidate)

    assert len(failures) == 9
    assert failures[0].startswith("overall:")
    assert "orders.py: missing" in failures[3]
