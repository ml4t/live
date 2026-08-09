"""Tests for dependency policy, audit, and compatibility tooling."""

from __future__ import annotations

import runpy
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

QUALIFICATION = Path(__file__).parents[2] / "scripts" / "qualification"
AUDIT = runpy.run_path(str(QUALIFICATION / "audit_dependencies.py"))
MATRIX = runpy.run_path(str(QUALIFICATION / "check_dependency_matrix.py"))


def test_dependency_closure_is_transitive() -> None:
    dependency_closure = cast(
        Callable[[dict[str, dict[str, Any]], list[str]], set[str]], AUDIT["dependency_closure"]
    )
    packages = {
        "root": {"dependencies": [{"name": "middle"}]},
        "middle": {"dependencies": [{"name": "leaf"}]},
        "leaf": {},
    }

    assert dependency_closure(packages, ["root"]) == {"root", "middle", "leaf"}


def test_import_scan_requires_an_explicit_distribution_map(tmp_path: Path) -> None:
    imported_distributions = cast(
        Callable[[Path, dict[str, str]], set[str]], AUDIT["imported_distributions"]
    )
    source = tmp_path / "module.py"
    source.write_text("import httpx\nfrom ml4t.specs import MarketEvent\n")

    assert imported_distributions(tmp_path, {"httpx": "httpx", "ml4t.specs": "ml4t-specs"}) == {
        "httpx",
        "ml4t-specs",
    }

    source.write_text("import undeclared_package\n")
    try:
        imported_distributions(tmp_path, {})
    except ValueError as error:
        assert "Undeclared external import undeclared_package" in str(error)
    else:
        raise AssertionError("undeclared import was accepted")


def test_license_detection_prefers_spdx_and_handles_legacy_metadata() -> None:
    license_identifiers = cast(Callable[[dict[str, Any]], set[str]], AUDIT["license_identifiers"])

    assert license_identifiers({"license_expression": "MIT-0 OR Apache-2.0"}) == {
        "MIT-0",
        "Apache-2.0",
    }
    assert license_identifiers(
        {
            "license_expression": None,
            "license": "Apache Software License v2",
            "classifiers": [],
        }
    ) == {"Apache-2.0"}


def test_seeded_advisory_is_a_blocking_policy_failure() -> None:
    package_policy_failures = cast(Callable[..., list[str]], AUDIT["package_policy_failures"])

    assert package_policy_failures("fixture", {"MIT"}, ["OSV-SEEDED-1"], {"MIT"}) == [
        "dependency fixture has advisories: ['OSV-SEEDED-1']"
    ]


def test_locked_profile_ignores_foreign_platform_dependencies() -> None:
    runtime_lock_overrides = cast(
        Callable[[dict[str, Any]], dict[str, str]], MATRIX["runtime_lock_overrides"]
    )
    lock = {
        "package": [
            {"name": "ml4t-live", "dependencies": [{"name": "root"}]},
            {
                "name": "root",
                "version": "1.0",
                "source": {"registry": "https://pypi.org/simple"},
                "dependencies": [
                    {"name": "linux-only", "marker": "sys_platform == 'linux'"},
                    {"name": "windows-only", "marker": "sys_platform == 'win32'"},
                ],
            },
            {
                "name": "linux-only",
                "version": "2.0",
                "source": {"registry": "https://pypi.org/simple"},
            },
            {
                "name": "windows-only",
                "version": "3.0",
                "source": {"registry": "https://pypi.org/simple"},
            },
        ]
    }

    assert runtime_lock_overrides(lock) == {"root": "root==1.0", "linux-only": "linux-only==2.0"}


def test_profile_requirements_exclude_non_matrix_optional_dependency() -> None:
    profile_requirements = cast(Callable[..., list[str]], MATRIX["profile_requirements"])
    policy = {
        "dependencies": {
            "core": {"minimum": "core==1"},
            "optional": {"minimum": "optional==1", "matrix": False},
        }
    }

    assert profile_requirements(policy, {}, "minimum") == ["core==1"]
