"""Tests for dependency policy, audit, and compatibility tooling."""

from __future__ import annotations

import runpy
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).parents[2]
QUALIFICATION = ROOT / "scripts" / "qualification"
AUDIT = runpy.run_path(str(QUALIFICATION / "audit_dependencies.py"))
MATRIX = runpy.run_path(str(QUALIFICATION / "check_dependency_matrix.py"))
SNAPSHOT = runpy.run_path(str(QUALIFICATION / "dependency_snapshot.py"))


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


def test_release_requirements_reject_vcs_prerelease_and_unbounded_ranges() -> None:
    release_requirement_failures = cast(
        Callable[[list[str]], list[str]], AUDIT["release_requirement_failures"]
    )

    failures = release_requirement_failures(
        [
            "vcs @ git+https://example.invalid/project.git@abc123",
            "preview==1.0.0b1",
            "unbounded>=1.0",
            "bounded>=1.0,<2",
            "exact==3.0",
        ]
    )

    assert any("vcs uses a direct URL" in failure for failure in failures)
    assert any("preview permits a prerelease" in failure for failure in failures)
    assert any("unbounded lacks a finite upper bound" in failure for failure in failures)
    assert not any(
        failure.startswith("dependency bounded") or failure.startswith("dependency exact")
        for failure in failures
    )


def test_release_lock_rejects_non_registry_and_prerelease_packages() -> None:
    release_lock_failures = cast(
        Callable[[dict[str, dict[str, Any]], set[str]], list[str]],
        AUDIT["release_lock_failures"],
    )
    packages = {
        "stable": {
            "version": "1.0.0",
            "source": {"registry": "https://pypi.org/simple"},
        },
        "preview": {
            "version": "2.0.0rc1",
            "source": {"registry": "https://pypi.org/simple"},
        },
        "vcs": {
            "version": "1.0.0",
            "source": {"git": "https://example.invalid/project.git#abc123"},
        },
    }

    failures = release_lock_failures(packages, set(packages))

    assert failures == [
        "locked runtime dependency preview is a prerelease: 2.0.0rc1",
        "locked runtime dependency vcs is not resolved from the release index",
    ]


def test_dependency_audit_includes_documentation_closure() -> None:
    scoped_closures = cast(
        Callable[[dict[str, Any]], dict[str, set[str]]], AUDIT["scoped_closures"]
    )
    lock = tomllib.loads((ROOT / "uv.lock").read_text())

    scopes = scoped_closures(lock)

    assert {"mkdocs", "mkdocs-material", "mkdocstrings"} <= scopes["documentation"]


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
                    {"name": "active", "marker": "python_version >= '3.0'"},
                    {"name": "foreign", "marker": "python_version < '2.0'"},
                ],
            },
            {
                "name": "active",
                "version": "2.0",
                "source": {"registry": "https://pypi.org/simple"},
            },
            {
                "name": "foreign",
                "version": "3.0",
                "source": {"registry": "https://pypi.org/simple"},
            },
        ]
    }

    assert runtime_lock_overrides(lock) == {"root": "root==1.0", "active": "active==2.0"}


def test_profile_requirements_exclude_non_matrix_optional_dependency() -> None:
    profile_requirements = cast(Callable[..., list[str]], MATRIX["profile_requirements"])
    policy = {
        "dependencies": {
            "core": {"minimum": "core==1"},
            "optional": {"minimum": "optional==1", "matrix": False},
        }
    }

    assert profile_requirements(policy, {}, "minimum") == ["core==1"]


def test_maximum_profile_resolves_the_full_supported_ml4t_ranges() -> None:
    policy = tomllib.loads((ROOT / "dependency-policy.toml").read_text())

    for name in ("ml4t-backtest", "ml4t-specs"):
        record = policy["dependencies"][name]
        assert record["maximum"] == record["requirement"]


def test_portable_snapshot_excludes_unrelated_upstream_exports() -> None:
    snapshot = cast(Callable[[], dict[str, Any]], SNAPSHOT["snapshot"])()
    portable_api = cast(dict[str, Any], snapshot["portable_api"])

    assert "live_public" in portable_api
    assert "backtest_public" not in portable_api
    assert "specs_public" not in portable_api
