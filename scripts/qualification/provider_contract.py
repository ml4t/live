"""Build provider-specific identities for reusable extended qualification evidence."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tomllib
from dataclasses import dataclass
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
_PAPER_SHARED_PATHS = (
    "src/ml4t/live/__init__.py",
    "src/ml4t/live/orders.py",
    "src/ml4t/live/persistence.py",
    "src/ml4t/live/protocols.py",
    "src/ml4t/live/safety.py",
    "src/ml4t/live/state_migration.py",
)
_V3_PATHS = {
    "alpaca": (*_V2_PATHS["alpaca"], *_PAPER_SHARED_PATHS),
    "ib": (*_V2_PATHS["ib"], *_PAPER_SHARED_PATHS),
    "okx": (
        *_V2_PATHS["okx"],
        "src/ml4t/live/__init__.py",
        "src/ml4t/live/persistence.py",
        "src/ml4t/live/protocols.py",
    ),
}
_DEPENDENCIES = {"alpaca": "alpaca-py", "ib": "ib-async", "okx": "ccxt"}
CURRENT_SCHEMA_VERSION = 3


class ProviderContractError(RuntimeError):
    """A provider contract cannot be computed or verified."""


@dataclass(frozen=True)
class ProviderContractClassification:
    """Explain whether retained provider evidence covers a candidate."""

    provider: str
    reusable: bool
    reason: str
    changed_inputs: tuple[str, ...] = ()


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


def _supported_runtime(pyproject_text: str) -> dict[str, Any]:
    project = tomllib.loads(pyproject_text).get("project")
    if not isinstance(project, dict):
        raise ProviderContractError("pyproject has no project table")
    requires_python = project.get("requires-python")
    classifiers = project.get("classifiers")
    if not isinstance(requires_python, str) or not isinstance(classifiers, list):
        raise ProviderContractError("pyproject has no supported Python runtime contract")
    python_classifiers = sorted(
        classifier
        for classifier in classifiers
        if isinstance(classifier, str)
        and classifier.startswith("Programming Language :: Python ::")
    )
    if not python_classifiers:
        raise ProviderContractError("pyproject has no Python runtime classifiers")
    return {
        **RUNTIME_CONTRACT,
        "requires_python": requires_python,
        "classifiers": python_classifiers,
    }


def provider_contract(
    provider: str,
    *,
    checkout_root: Path,
    ref: str = "HEAD",
    schema_version: int = CURRENT_SCHEMA_VERSION,
) -> dict[str, Any]:
    """Return a deterministic provider contract at a Git revision."""
    if provider not in PROVIDERS:
        raise ProviderContractError(f"unsupported provider: {provider}")
    if schema_version not in {1, 2, CURRENT_SCHEMA_VERSION}:
        raise ProviderContractError(f"unsupported provider contract schema: {schema_version}")
    paths = (
        _V1_PATHS[provider]
        if schema_version == 1
        else _V2_PATHS[provider]
        if schema_version == 2
        else _V3_PATHS[provider]
    )
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
        "runtime": (
            RUNTIME_CONTRACT
            if schema_version < CURRENT_SCHEMA_VERSION
            else _supported_runtime(_git_text(checkout_root, ref, "pyproject.toml"))
        ),
    }
    digest = hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    return {**payload, "sha256": digest}


def _contract_changes(evidence: dict[str, Any], candidate: dict[str, Any]) -> tuple[str, ...]:
    changed = [
        path
        for path in sorted(set(evidence["inputs"]) | set(candidate["inputs"]))
        if evidence["inputs"].get(path) != candidate["inputs"].get(path)
    ]
    if evidence["dependency"] != candidate["dependency"]:
        changed.append(f"dependency:{candidate['dependency']['name']}")
    if evidence["runtime"] != candidate["runtime"]:
        changed.append("supported-runtime")
    return tuple(changed)


def classify_provider_contract(
    provider: str,
    *,
    evidence_commit: str,
    candidate_commit: str,
    checkout_root: Path,
    reported_contract: dict[str, Any] | None,
) -> ProviderContractClassification:
    """Classify retained evidence against the current provider contract."""
    if provider not in PROVIDERS:
        return ProviderContractClassification(provider, False, "unsupported provider")
    if reported_contract is None:
        if evidence_commit not in LEGACY_EVIDENCE_COMMITS:
            return ProviderContractClassification(
                provider, False, "retained evidence has no provider contract"
            )
        reported_schema = 1
    else:
        reported_schema = reported_contract.get("schema_version")
        if not isinstance(reported_schema, int) or reported_schema not in {
            2,
            CURRENT_SCHEMA_VERSION,
        }:
            return ProviderContractClassification(
                provider, False, "retained evidence uses an unsupported contract schema"
            )
    try:
        reported_evidence = provider_contract(
            provider,
            checkout_root=checkout_root,
            ref=evidence_commit,
            schema_version=reported_schema,
        )
        if reported_contract is not None and reported_contract != reported_evidence:
            return ProviderContractClassification(
                provider, False, "reported contract does not match its evidence commit"
            )
        evidence = provider_contract(
            provider,
            checkout_root=checkout_root,
            ref=evidence_commit,
        )
        candidate = provider_contract(
            provider,
            checkout_root=checkout_root,
            ref=candidate_commit,
        )
    except (ProviderContractError, tomllib.TOMLDecodeError) as error:
        return ProviderContractClassification(provider, False, str(error))
    changed_inputs = _contract_changes(evidence, candidate)
    if changed_inputs:
        return ProviderContractClassification(
            provider,
            False,
            "provider contract changed",
            changed_inputs,
        )
    return ProviderContractClassification(provider, True, "provider contract unchanged")


def provider_contract_matches(
    provider: str,
    *,
    evidence_commit: str,
    candidate_commit: str,
    checkout_root: Path,
    reported_contract: dict[str, Any] | None,
) -> bool:
    """Return whether extended evidence still covers the candidate contract."""
    return classify_provider_contract(
        provider,
        evidence_commit=evidence_commit,
        candidate_commit=candidate_commit,
        checkout_root=checkout_root,
        reported_contract=reported_contract,
    ).reusable
