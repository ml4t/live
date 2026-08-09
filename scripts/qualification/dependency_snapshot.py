"""Emit the portable API snapshot used by dependency compatibility tests."""

from __future__ import annotations

import json
from dataclasses import fields
from importlib import metadata
from inspect import signature

import ml4t.backtest as backtest
import ml4t.specs as specs

import ml4t.live as live

DIRECT_DISTRIBUTIONS = (
    "alpaca-py",
    "ccxt",
    "httpx",
    "ib-async",
    "ml4t-backtest",
    "ml4t-live",
    "ml4t-specs",
)


def public_names(module: object) -> list[str]:
    return sorted(name for name in vars(module) if not name.startswith("_"))


def callable_signatures(owner: type, names: tuple[str, ...]) -> dict[str, str]:
    return {name: str(signature(getattr(owner, name))) for name in names}


def snapshot() -> dict[str, object]:
    portable_api = {
        "backtest_public": public_names(backtest),
        "live_public": public_names(live),
        "specs_public": public_names(specs),
        "strategy_callbacks": callable_signatures(
            backtest.Strategy, ("on_start", "on_prepare", "on_data", "on_end")
        ),
        "market_event_fields": [field.name for field in fields(specs.MarketEvent)],
        "target_intent_fields": [field.name for field in fields(specs.CanonicalTargetIntent)],
        "lifecycle_version": specs.LIFECYCLE_V1.version.value,
        "event_completion": [member.value for member in specs.EventCompletion],
        "event_kinds": [member.value for member in specs.MarketEventKind],
    }
    return {
        "portable_api": portable_api,
        "versions": {name: metadata.version(name) for name in DIRECT_DISTRIBUTIONS},
    }


if __name__ == "__main__":
    print(json.dumps(snapshot(), sort_keys=True))
