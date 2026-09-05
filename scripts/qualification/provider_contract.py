"""Build provider-specific identities for reusable extended qualification evidence."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tomllib
from pathlib import Path
from typing import Any

PROVIDERS = ("alpaca", "ib", "okx")
LEGACY_EVIDENCE_COMMITS = frozenset({"98c414e9d858427c31e1680faccdc8dca498bf6b"})
RUNTIME_CONTRACT = {
    "implementation": "CPython",
    "python": "3.12",
    "system": "Linux",
    "machine": "x86_64",
}

_V1_PATHS = {
    "alpaca": ("src/ml4t/live/brokers/alpaca.py",),
    "ib": ("src/ml4t/live/brokers/ib.py",),
    "okx": ("src/ml4t/live/feeds/okx_feed.py",),
}
_V2_PATHS = {
    "alpaca": (*_V1_PATHS["alpaca"], "scripts/qualification/qualify_paper.py"),
    "ib": (*_V1_PATHS["ib"], "scripts/qualification/qualify_paper.py"),
    "okx": (
        *_V1_PATHS["okx"],
        "src/ml4t/live/feeds/events.py",
        "src/ml4t/live/feeds/queue.py",
        "scripts/qualification/qualify_feeds.py",
    ),
}
_DEPENDENCIES = {"alpaca": "alpaca-py", "ib": "ib-async", "okx": "ccxt"}


class ProviderContractError(RuntimeError):
    """A provider contract cannot be computed or verified."""


def _git_text(checkout_root: Path, ref: str, path: str) -> str:
    result = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        cwd=checkout_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ProviderContractError(f"provider contract input is unavailable: {path}")
    return result.stdout


def _dependency_version(lock_text: str, dependency: str) -> str:
    lock = tomllib.loads(lock_text)
    matches = {
        package.get("version")
        for package in lock.get("package", [])
        if package.get("name") == dependency
    }
    if len(matches) != 1 or not all(isinstance(version, str) for version in matches):
        raise ProviderContractError(
            f"provider dependency has no unique resolved version: {dependency}"
        )
    return str(matches.pop())


def provider_contract(
    provider: str,
    *,
    checkout_root: Path,
    ref: str = "HEAD",
    schema_version: int = 2,
) -> dict[str, Any]:
    """Return a deterministic provider contract at a Git revision."""
    if provider not in PROVIDERS:
        raise ProviderContractError(f"unsupported provider: {provider}")
    if schema_version not in {1, 2}:
        raise ProviderContractError(f"unsupported provider contract schema: {schema_version}")
    paths = _V1_PATHS[provider] if schema_version == 1 else _V2_PATHS[provider]
    inputs = {
        path: hashlib.sha256(_git_text(checkout_root, ref, path).encode()).hexdigest()
        for path in paths
    }
    dependency = _DEPENDENCIES[provider]
    payload = {
        "schema_version": schema_version,
        "provider": provider,
        "inputs": inputs,
        "dependency": {
            "name": dependency,
            "version": _dependency_version(_git_text(checkout_root, ref, "uv.lock"), dependency),
        },
        "runtime": RUNTIME_CONTRACT,
    }
    digest = hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    return {**payload, "sha256": digest}


def provider_contract_matches(
    provider: str,
    *,
    evidence_commit: str,
    candidate_commit: str,
    checkout_root: Path,
    reported_contract: dict[str, Any] | None,
) -> bool:
    """Return whether extended evidence still covers the candidate contract."""
    if reported_contract is None:
        if evidence_commit not in LEGACY_EVIDENCE_COMMITS:
            return False
        schema_version = 1
    else:
        schema_version = reported_contract.get("schema_version")
        if schema_version != 2:
            return False
    try:
        evidence = provider_contract(
            provider,
            checkout_root=checkout_root,
            ref=evidence_commit,
            schema_version=schema_version,
        )
        candidate = provider_contract(
            provider,
            checkout_root=checkout_root,
            ref=candidate_commit,
            schema_version=schema_version,
        )
    except ProviderContractError:
        return False
    if reported_contract is not None and reported_contract != evidence:
        return False
    return evidence["sha256"] == candidate["sha256"]
