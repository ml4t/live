"""Build provider-specific identities for reusable extended qualification evidence."""

from __future__ import annotations

import ast
import hashlib
import importlib.metadata
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
_CALENDAR_RUNTIME_PATHS = (
    "src/ml4t/live/engine.py",
    "src/ml4t/live/runtime.py",
    "src/ml4t/live/lifecycle.py",
    "src/ml4t/live/temporal.py",
    "scripts/qualification/provider_contract.py",
    "scripts/qualification/qualify_paper.py",
    ".github/workflows/paper.yml",
)
_V4_PATHS = {
    provider: tuple(dict.fromkeys((*paths, *_CALENDAR_RUNTIME_PATHS)))
    for provider, paths in _V3_PATHS.items()
}
_SHARED_DEPENDENCIES = (
    "ml4t-backtest",
    "ml4t-specs",
    "numpy",
    "pandas",
    "pandas-market-calendars",
    "exchange-calendars",
)
CURRENT_SCHEMA_VERSION = 4


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


def _input_digest(checkout_root: Path, ref: str, path: str) -> str | None:
    try:
        return hashlib.sha256(_git_text(checkout_root, ref, path).encode()).hexdigest()
    except ProviderContractError:
        if path not in _CALENDAR_RUNTIME_PATHS:
            raise
        tree = subprocess.run(
            ["git", "ls-tree", ref, "--", path],
            cwd=checkout_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if tree.returncode == 0 and not tree.stdout.strip():
            return None
        raise


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


def verify_installed_runtime(*, checkout_root: Path, ref: str) -> dict[str, str]:
    """Reject provider evidence whose installed inputs differ from its source lock."""
    lock_text = _git_text(checkout_root, ref, "uv.lock")
    installed = {}
    for name in (*_SHARED_DEPENDENCIES, *_DEPENDENCIES.values()):
        expected = _dependency_version(lock_text, name)
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as error:
            raise ProviderContractError(f"installed provider runtime is missing {name}") from error
        if actual != expected:
            raise ProviderContractError(
                f"installed provider runtime differs from candidate lock: {name} "
                f"expected {expected}, found {actual}"
            )
        installed[name] = actual
    return installed


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
    if schema_version not in {1, 2, 3, CURRENT_SCHEMA_VERSION}:
        raise ProviderContractError(f"unsupported provider contract schema: {schema_version}")
    paths = (
        _V1_PATHS[provider]
        if schema_version == 1
        else _V2_PATHS[provider]
        if schema_version == 2
        else _V3_PATHS[provider]
        if schema_version == 3
        else _V4_PATHS[provider]
    )
    inputs = {path: _input_digest(checkout_root, ref, path) for path in paths}
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
            if schema_version < 3
            else _supported_runtime(_git_text(checkout_root, ref, "pyproject.toml"))
        ),
    }
    if schema_version >= 4:
        lock_text = _git_text(checkout_root, ref, "uv.lock")
        payload["shared_dependencies"] = {
            name: _dependency_version(lock_text, name) for name in _SHARED_DEPENDENCIES
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
    for name in sorted(
        set(evidence.get("shared_dependencies", {})) | set(candidate.get("shared_dependencies", {}))
    ):
        if evidence.get("shared_dependencies", {}).get(name) != candidate.get(
            "shared_dependencies", {}
        ).get(name):
            changed.append(f"dependency:{name}")
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
            3,
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


_BROKER_SNAPSHOT_FUNCTIONS = (
    "_build_broker",
    "_number",
    "_pending_ib_statuses",
    "_order_type",
    "_adapter_snapshot",
    "_vendor_snapshot",
    "_snapshot",
)
_BROKER_DURATION_FUNCTIONS = (
    *_BROKER_SNAPSHOT_FUNCTIONS,
    "_provider_state_checksum",
    "_rss_bytes",
    "_soak_snapshot",
    "_sleep_until",
    "_connect_for_controlled_reconnect",
    "run_provider_soak",
)
_BROKER_COMPATIBILITY_FUNCTIONS = (
    *_BROKER_SNAPSHOT_FUNCTIONS,
    "_raw_tagged_orders",
    "_adapter_id_for_tag",
    "_wait_for_tag_count",
    "_tag_keyword",
    "_assert_atomic_rejections",
    "_cleanup_tags",
    "_exercise_provider",
    "_restart_provider",
    "run_provider_phase",
)


def _semantic_ast(value: Any) -> Any:
    if isinstance(value, ast.AST):
        return {
            "node": type(value).__name__,
            "fields": {
                name: _semantic_ast(field)
                for name, field in ast.iter_fields(value)
                if field is not None and field != []
            },
        }
    if isinstance(value, list):
        return [_semantic_ast(item) for item in value]
    if value is Ellipsis:
        return {"literal": "ellipsis"}
    return value


def _operation_identity(node: ast.AST) -> str:
    return json.dumps(_semantic_ast(node), sort_keys=True, separators=(",", ":"))


def provider_stage_inputs(
    provider: str, stage: str, *, checkout_root: Path, ref: str
) -> dict[str, Any]:
    """Identify operations exercised by compatibility or connection-duration evidence."""
    if provider not in PROVIDERS or stage not in {"compatibility", "connection-duration"}:
        raise ProviderContractError("unsupported provider evidence stage")
    if provider == "okx":
        paths = tuple(path for path in _V3_PATHS[provider] if not path.startswith("scripts/"))
        script = "scripts/qualification/qualify_feeds.py"
        names = (
            (
                "_rss_bytes",
                "_soak_snapshot",
                "qualify_okx_soak",
                "_native_snapshot",
                "_native_matches",
                "_event_sequence",
                "_validate_okx_event",
                "_close_within_limit",
            )
            if stage == "connection-duration"
            else (
                "qualify_okx",
                "_native_snapshot",
                "_native_matches",
                "_event_sequence",
                "_validate_okx_event",
                "_collect_initial",
                "_collect_next_complete",
                "_close_within_limit",
                "_qualify_overload",
            )
        )
    else:
        paths = (
            (*_V1_PATHS[provider], "src/ml4t/live/orders.py", "src/ml4t/live/protocols.py")
            if stage == "connection-duration"
            else (*_V1_PATHS[provider], *_PAPER_SHARED_PATHS)
        )
        script = "scripts/qualification/qualify_paper.py"
        names = (
            _BROKER_DURATION_FUNCTIONS
            if stage == "connection-duration"
            else _BROKER_COMPATIBILITY_FUNCTIONS
        )
    tree = ast.parse(_git_text(checkout_root, ref, script))
    functions = {
        node.name: hashlib.sha256(_operation_identity(node).encode()).hexdigest()
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    }
    if set(functions) != set(names):
        raise ProviderContractError("provider stage has missing qualification operations")
    constants = {
        node.targets[0].id: _operation_identity(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and (node.targets[0].id.startswith("SOAK_") or node.targets[0].id in {"ASSET", "QUANTITY"})
    }
    return {
        "files": {path: _input_digest(checkout_root, ref, path) for path in paths},
        "qualification_script": script,
        "functions": functions,
        "constants": constants,
    }
