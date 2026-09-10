"""Fail closed unless every required paper qualification outcome succeeded."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

SHORT_OUTCOMES = (
    "alpaca-exercise",
    "alpaca-restart",
    "ib-exercise",
    "ib-restart",
    "okx-external",
    "paper-evidence-scan",
    "feed-evidence-scan",
)


def outcome_failures(outcomes: dict[str, str], extended_provider: str) -> list[str]:
    """Return required qualification stages whose outcomes are not successful."""
    required = list(SHORT_OUTCOMES)
    if extended_provider != "none":
        required.append("provider-soaks")
    if extended_provider == "all":
        required.extend(("paper-evidence", "feed-evidence"))
    elif extended_provider == "okx":
        required.append("feed-evidence")
    return [name for name in required if outcomes.get(name) != "success"]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--extended-provider",
        choices=("none", "alpaca", "ib", "okx", "all"),
        required=True,
    )
    for name in (*SHORT_OUTCOMES, "provider-soaks", "paper-evidence", "feed-evidence"):
        parser.add_argument(f"--{name}", required=True)
    args = parser.parse_args(argv)
    outcomes = {
        name: getattr(args, name.replace("-", "_"))
        for name in (*SHORT_OUTCOMES, "provider-soaks", "paper-evidence", "feed-evidence")
    }
    failures = outcome_failures(outcomes, args.extended_provider)
    if failures:
        print("paper qualification failed: " + ", ".join(failures))
        return 1
    print("paper qualification: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
