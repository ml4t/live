"""Run the stable provider soaks concurrently and retain redacted reports."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

from scripts.qualification.qualify_feeds import qualify_okx_soak
from scripts.qualification.qualify_paper import _load_json, _write_json, run_provider_soak


async def qualify_provider_soaks(
    *, candidate: dict[str, Any], checkout_root: Path, providers: tuple[str, ...]
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    tasks = {
        provider: (
            qualify_okx_soak(candidate, checkout_root)
            if provider == "okx"
            else run_provider_soak(
                provider=provider,
                candidate=candidate,
                checkout_root=checkout_root,
            )
        )
        for provider in providers
    }
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    reports: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    for provider, result in zip(tasks, results, strict=True):
        if isinstance(result, BaseException):
            failures.append(f"{provider}:{type(result).__name__}")
        else:
            reports[provider] = result
            if result.get("passed") is not True:
                failures.append(f"{provider}:failed-report")
    return reports, failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--checkout-root", type=Path, required=True)
    parser.add_argument("--alpaca-output", type=Path, required=True)
    parser.add_argument("--ib-output", type=Path, required=True)
    parser.add_argument("--okx-output", type=Path, required=True)
    parser.add_argument(
        "--provider",
        choices=("alpaca", "ib", "okx", "all"),
        default="all",
    )
    args = parser.parse_args()
    providers = ("alpaca", "ib", "okx") if args.provider == "all" else (args.provider,)
    reports, failures = asyncio.run(
        qualify_provider_soaks(
            candidate=_load_json(args.candidate),
            checkout_root=args.checkout_root,
            providers=providers,
        )
    )
    outputs = {
        "alpaca": args.alpaca_output,
        "ib": args.ib_output,
        "okx": args.okx_output,
    }
    for provider, report in reports.items():
        _write_json(outputs[provider], report)
    if failures:
        print("provider soaks: FAIL (" + ", ".join(failures) + ")")
        return 1
    print("provider soaks: PASS (" + ", ".join(providers) + ")")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
