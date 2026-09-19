"""Fail closed unless every required paper qualification outcome succeeded."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

ALL_OUTCOMES = (
    "alpaca-exercise",
    "alpaca-restart",
    "ib-exercise",
    "ib-restart",
    "okx-external",
    "paper-evidence-scan",
    "feed-evidence-scan",
)
PROVIDER_OUTCOMES = {
    "alpaca": ("alpaca-exercise", "alpaca-restart"),
    "ib": ("ib-exercise", "ib-restart"),
    "okx": ("okx-external",),
}


def outcome_failures(outcomes: dict[str, str], extended_provider: str) -> list[str]:
    """Return required qualification stages whose outcomes are not successful."""
    selected = (
        tuple(name for names in PROVIDER_OUTCOMES.values() for name in names)
        if extended_provider == "all"
        else PROVIDER_OUTCOMES[extended_provider]
    )
    required = [*selected, "paper-evidence-scan", "feed-evidence-scan", "provider-soaks"]
    if extended_provider == "all":
        required.extend(("paper-evidence", "feed-evidence"))
    elif extended_provider == "okx":
        required.append("feed-evidence")
    return [name for name in required if outcomes.get(name) != "success"]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--extended-provider",
        choices=("alpaca", "ib", "okx", "all"),
        required=True,
    )
    for name in (*ALL_OUTCOMES, "provider-soaks", "paper-evidence", "feed-evidence"):
        parser.add_argument(f"--{name}", required=True)
    args = parser.parse_args(argv)
    outcomes = {
        name: getattr(args, name.replace("-", "_"))
        for name in (*ALL_OUTCOMES, "provider-soaks", "paper-evidence", "feed-evidence")
    }
    failures = outcome_failures(outcomes, args.extended_provider)
    if failures:
        print("paper qualification failed: " + ", ".join(failures))
        return 1
    print("paper qualification: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
