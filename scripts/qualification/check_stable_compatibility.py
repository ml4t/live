"""Reject unreviewed changes to the installed ml4t-live stable surface."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

CONTRACT_FIELDS = (
    "kind",
    "signature",
    "bases",
    "methods",
    "dataclass_fields",
    "enum_members",
)


def stable_symbols(surface: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (item["module"], item["name"]): item
        for item in surface.get("symbols", [])
        if item.get("classification") == "stable"
    }


def short(value: Any, limit: int = 300) -> str:
    rendered = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return rendered if len(rendered) <= limit else f"{rendered[:limit]}..."


def compare_value(path: str, expected: Any, actual: Any) -> str | None:
    if expected == actual:
        return None
    return f"{path} changed: expected {short(expected)}, found {short(actual)}"


def compare_surfaces(baseline: dict[str, Any], candidate: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if baseline.get("schema_version") != candidate.get("schema_version"):
        failures.append(
            "surface schema_version changed: "
            f"expected {baseline.get('schema_version')!r}, "
            f"found {candidate.get('schema_version')!r}"
        )
    if baseline.get("distribution", {}).get("name") != candidate.get("distribution", {}).get(
        "name"
    ):
        failures.append("candidate distribution name does not match the baseline")

    expected_symbols = stable_symbols(baseline)
    actual_symbols = stable_symbols(candidate)
    for module_name, name in sorted(expected_symbols.keys() - actual_symbols.keys()):
        failures.append(f"removed stable symbol {module_name}:{name}")
    for module_name, name in sorted(actual_symbols.keys() - expected_symbols.keys()):
        failures.append(f"unbaselined stable symbol {module_name}:{name}")
    for key in sorted(expected_symbols.keys() & actual_symbols.keys()):
        expected = expected_symbols[key]
        actual = actual_symbols[key]
        label = f"{key[0]}:{key[1]}"
        for field in CONTRACT_FIELDS:
            difference = compare_value(f"{label}.{field}", expected.get(field), actual.get(field))
            if difference:
                failures.append(difference)

    for field in ("cli", "entry_points", "persisted_schemas"):
        difference = compare_value(field, baseline.get(field), candidate.get(field))
        if difference:
            failures.append(difference)
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    baseline = json.loads(args.baseline.read_text())
    candidate = json.loads(args.candidate.read_text())
    failures = compare_surfaces(baseline, candidate)
    report = {
        "schema_version": 1,
        "baseline": str(args.baseline.resolve()),
        "candidate": str(args.candidate.resolve()),
        "result": "fail" if failures else "pass",
        "failures": failures,
    }
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(rendered, end="")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
