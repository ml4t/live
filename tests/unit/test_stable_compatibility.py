"""Fault-sensitive tests for the installed stable compatibility gate."""

from __future__ import annotations

import copy
import runpy
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).parents[2]
CHECKER = runpy.run_path(str(ROOT / "scripts" / "qualification" / "check_stable_compatibility.py"))
compare_surfaces = cast(
    Callable[[dict[str, Any], dict[str, Any]], list[str]], CHECKER["compare_surfaces"]
)


def test_compatibility_policy_requires_installed_semver_contract() -> None:
    policy = tomllib.loads((ROOT / "compatibility-policy.toml").read_text())

    assert policy["versioning"] == "Semantic Versioning 2.0.0"
    assert policy["change-control"]["installed-artifact-required"] is True
    assert policy["change-control"]["source-checkout-result-accepted"] is False
    assert policy["deprecation"]["minimum-prior-minor-releases"] >= 1
    assert policy["deprecation"]["migration-required"] is True
    assert policy["first-stable-release"]["all-beta-incompatibilities-require-migration"] is True


def surface() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "distribution": {"name": "ml4t-live", "version": "0.1.0"},
        "root_exports": ["Config", "Failure"],
        "symbols": [
            {
                "module": "ml4t.live",
                "name": "Config",
                "classification": "stable",
                "kind": "dataclass",
                "defined_in": "ml4t.live.config",
                "signature": "(enabled: bool = False)",
                "bases": ["builtins.object"],
                "methods": [],
                "dataclass_fields": [{"name": "enabled", "type": "bool", "default": "False"}],
            },
            {
                "module": "ml4t.live",
                "name": "Failure",
                "classification": "stable",
                "kind": "exception",
                "defined_in": "ml4t.live.errors",
                "signature": "()",
                "bases": ["builtins.RuntimeError"],
                "methods": [],
            },
            {
                "module": "ml4t.live",
                "name": "Preview",
                "classification": "experimental",
                "kind": "class",
                "defined_in": "ml4t.live.preview",
                "signature": "()",
                "bases": ["builtins.object"],
                "methods": [],
            },
        ],
        "cli": {"status": {"classification": "stable", "arguments": []}},
        "entry_points": {"ml4t-live": "ml4t.live.cli.main:app"},
        "persisted_schemas": {"risk_state": {"version": 1, "fields": ["enabled"]}},
    }


def test_identical_surface_passes_even_when_version_changes() -> None:
    baseline = surface()
    candidate = copy.deepcopy(baseline)
    candidate["distribution"]["version"] = "0.1.1"

    assert compare_surfaces(baseline, candidate) == []


def test_removal_signature_default_exception_and_schema_changes_fail() -> None:
    baseline = surface()
    candidate = copy.deepcopy(baseline)
    candidate["symbols"] = [item for item in candidate["symbols"] if item["name"] != "Config"]
    failure = next(item for item in candidate["symbols"] if item["name"] == "Failure")
    failure["signature"] = "(message: str)"
    failure["bases"] = ["builtins.Exception"]
    candidate["persisted_schemas"]["risk_state"]["fields"].append("new_field")

    failures = compare_surfaces(baseline, candidate)

    assert any("removed stable symbol ml4t.live:Config" in item for item in failures)
    assert any("ml4t.live:Failure.signature" in item for item in failures)
    assert any("ml4t.live:Failure.bases" in item for item in failures)
    assert any("persisted_schemas" in item for item in failures)


def test_default_change_and_unbaselined_stable_symbol_fail() -> None:
    baseline = surface()
    candidate = copy.deepcopy(baseline)
    config = next(item for item in candidate["symbols"] if item["name"] == "Config")
    config["dataclass_fields"][0]["default"] = "True"
    candidate["symbols"].append(
        {
            "module": "ml4t.live",
            "name": "NewStable",
            "classification": "stable",
            "kind": "class",
            "defined_in": "ml4t.live.new",
            "signature": "()",
            "bases": ["builtins.object"],
            "methods": [],
        }
    )

    failures = compare_surfaces(baseline, candidate)

    assert any("ml4t.live:Config.dataclass_fields" in item for item in failures)
    assert any("unbaselined stable symbol ml4t.live:NewStable" in item for item in failures)


def test_experimental_changes_do_not_create_stable_failures() -> None:
    baseline = surface()
    candidate = copy.deepcopy(baseline)
    preview = next(item for item in candidate["symbols"] if item["name"] == "Preview")
    preview["signature"] = "(changed: bool = True)"

    assert compare_surfaces(baseline, candidate) == []


def test_cli_and_entry_point_changes_fail() -> None:
    baseline = surface()
    candidate = copy.deepcopy(baseline)
    candidate["cli"]["status"]["arguments"].append({"dest": "format"})
    candidate["entry_points"]["ml4t-live"] = "ml4t.live.cli.main:run_cli"

    failures = compare_surfaces(baseline, candidate)

    assert any("cli" in item for item in failures)
    assert any("entry_points" in item for item in failures)
