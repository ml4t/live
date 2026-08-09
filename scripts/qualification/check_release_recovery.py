"""Validate immutable release-recovery procedures."""

from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = REPOSITORY_ROOT / "release-recovery.toml"
REQUIRED_SCENARIOS = {
    "partial_pypi_publish",
    "pypi_succeeded_github_failed",
    "tag_conflict",
    "pypi_version_conflict",
    "github_release_conflict",
    "provenance_failure",
}


def recovery_failures(policy: dict[str, Any]) -> list[str]:
    """Return structural or unsafe recovery-policy failures."""
    failures: list[str] = []
    if policy.get("schema_version") != 1:
        failures.append("release recovery schema must be version 1")
    scenarios = policy.get("scenarios")
    if not isinstance(scenarios, dict):
        return [*failures, "release recovery scenarios must be a mapping"]
    if set(scenarios) != REQUIRED_SCENARIOS:
        failures.append(
            "release recovery scenarios differ: "
            f"missing={sorted(REQUIRED_SCENARIOS - set(scenarios))}; "
            f"unexpected={sorted(set(scenarios) - REQUIRED_SCENARIOS)}"
        )
    for name, scenario in scenarios.items():
        if not isinstance(scenario, dict):
            failures.append(f"{name} recovery must be a mapping")
            continue
        if scenario.get("immutable") is not True:
            failures.append(f"{name} recovery does not preserve immutable records")
        if scenario.get("replacement_allowed") is not False:
            failures.append(f"{name} recovery permits replacement")
        if scenario.get("requires_new_version_on_conflict") is not True:
            failures.append(f"{name} recovery does not require a new version after conflict")
        procedure = scenario.get("procedure")
        if (
            not isinstance(procedure, list)
            or len(procedure) < 3
            or not all(isinstance(step, str) and step.strip() for step in procedure)
        ):
            failures.append(f"{name} recovery has no complete procedure")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    policy = tomllib.loads(POLICY_PATH.read_text())
    failures = recovery_failures(policy)
    report = {
        "schema_version": 1,
        "policy": str(POLICY_PATH.relative_to(REPOSITORY_ROOT)),
        "scenarios": sorted(policy.get("scenarios", {})),
        "failures": failures,
        "passed": not failures,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"release recovery policy: {'PASS' if not failures else 'FAIL'}")
    for failure in failures:
        print(f"- {failure}")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
