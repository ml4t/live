"""Audit the exact runtime and build dependency closures."""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import tomllib
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PYPI_PROJECT = "https://pypi.org/pypi/{name}/json"
OSV_BATCH = "https://api.osv.dev/v1/querybatch"

LICENSE_ALIASES = (
    ("APSL-2.0", ("APSL-2.0", "Apple Public Source License 2.0")),
    (
        "Apache-2.0",
        (
            "Apache-2.0",
            "Apache 2.0",
            "Apache License 2.0",
            "Apache Software License",
        ),
    ),
    ("BSD-3-Clause", ("BSD-3-Clause", "BSD 3-Clause", "BSD License")),
    ("BSD-2-Clause", ("BSD-2-Clause", "BSD 2-Clause")),
    ("MPL-2.0", ("MPL-2.0", "Mozilla Public License 2.0")),
    ("PSF-2.0", ("PSF-2.0", "Python Software Foundation License")),
    ("MIT", ("MIT",)),
    ("ISC", ("ISC",)),
    ("BSD", ("BSD",)),
)


def canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_name(requirement: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9_.-]+)", requirement)
    if match is None:
        raise ValueError(f"Invalid requirement: {requirement}")
    return canonical_name(match.group(1))


def package_index(lock: dict[str, Any]) -> dict[str, dict[str, Any]]:
    packages: dict[str, dict[str, Any]] = {}
    for package in lock["package"]:
        name = canonical_name(package["name"])
        if name in packages:
            raise ValueError(f"Multiple locked records for {name}; marker-aware audit is required")
        packages[name] = package
    return packages


def dependency_closure(packages: dict[str, dict[str, Any]], roots: list[str]) -> set[str]:
    closure: set[str] = set()
    pending = [canonical_name(root) for root in roots]
    while pending:
        name = pending.pop()
        if name in closure:
            continue
        if name not in packages:
            raise ValueError(f"Dependency {name} is absent from uv.lock")
        closure.add(name)
        pending.extend(dependency["name"] for dependency in packages[name].get("dependencies", []))
    return closure


def scoped_closures(lock: dict[str, Any]) -> dict[str, set[str]]:
    packages = package_index(lock)
    project = packages["ml4t-live"]
    scopes = {
        "runtime": dependency_closure(
            packages, [dependency["name"] for dependency in project["dependencies"]]
        ),
        "experimental": dependency_closure(
            packages,
            [
                dependency["name"]
                for dependency in project.get("optional-dependencies", {}).get("experimental", [])
            ],
        ),
        "build": dependency_closure(
            packages,
            [
                dependency["name"]
                for dependency in project.get("dev-dependencies", {}).get("build", [])
            ],
        ),
    }
    return scopes


def imported_distributions(source_root: Path, import_map: dict[str, str]) -> set[str]:
    imported = set()
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module]
            else:
                continue
            for module in modules:
                if module == "ml4t.live" or module.startswith("ml4t.live."):
                    continue
                root = module.split(".", 1)[0]
                if root in sys.stdlib_module_names:
                    continue
                candidates = [
                    key for key in import_map if module == key or module.startswith(f"{key}.")
                ]
                if not candidates:
                    raise ValueError(f"Undeclared external import {module} in {path}")
                key = max(candidates, key=len)
                imported.add(canonical_name(import_map[key]))
    return imported


def declared_requirements(project: dict[str, Any]) -> dict[str, str]:
    requirements = list(project["project"]["dependencies"])
    requirements.extend(project["project"].get("optional-dependencies", {}).get("experimental", []))
    return {requirement_name(requirement): requirement for requirement in requirements}


def validate_policy_declarations(
    project: dict[str, Any], policy: dict[str, Any], imported: set[str]
) -> list[str]:
    failures = []
    declared = declared_requirements(project)
    expected = {
        canonical_name(name): record["requirement"]
        for name, record in policy["dependencies"].items()
    }
    if declared != expected:
        failures.append(f"runtime declarations differ: declared={declared!r}, policy={expected!r}")
    missing_imports = imported - declared.keys()
    if missing_imports:
        failures.append(f"direct imports lack declarations: {sorted(missing_imports)}")

    build_system = {
        requirement_name(requirement): requirement
        for requirement in project["build-system"]["requires"]
    }
    expected_build = {
        canonical_name(name): record["requirement"]
        for name, record in policy["build-dependencies"].items()
    }
    if build_system != expected_build:
        failures.append(
            f"build declarations differ: declared={build_system!r}, policy={expected_build!r}"
        )
    return failures


def locked_requirement(name: str, package: dict[str, Any]) -> str:
    source = package.get("source", {})
    if "registry" in source:
        return f"{name}=={package['version']}"
    if "git" in source:
        git_url = source["git"].split("#", 1)[0].replace("?rev=", "@")
        return f"{name} @ git+{git_url.removeprefix('git+')}"
    raise ValueError(f"Unsupported locked source for {name}: {source}")


def validate_locked_policy(
    packages: dict[str, dict[str, Any]], policy: dict[str, Any]
) -> list[str]:
    failures = []
    for raw_name, record in policy["dependencies"].items():
        name = canonical_name(raw_name)
        actual = locked_requirement(name, packages[name])
        if record["locked"] != actual:
            failures.append(
                f"locked policy differs for {name}: policy={record['locked']!r}, lock={actual!r}"
            )
    return failures


def validate_build_constraints(
    packages: dict[str, dict[str, Any]], build_closure: set[str], path: Path
) -> list[str]:
    expected = sorted(locked_requirement(name, packages[name]) for name in build_closure)
    actual = sorted(line.strip() for line in path.read_text().splitlines() if line.strip())
    if actual == expected:
        return []
    return [f"build constraints differ: constraints={actual!r}, lock={expected!r}"]


def license_identifiers(info: dict[str, Any]) -> set[str]:
    expression = info.get("license_expression")
    if expression:
        return {
            token
            for token in re.findall(r"[A-Za-z0-9.-]+", expression)
            if token not in {"AND", "OR", "WITH"}
        }

    license_text = (info.get("license") or "").strip()
    search_values = [license_text] if len(license_text) <= 500 else []
    search_values.extend(
        classifier
        for classifier in info.get("classifiers", [])
        if classifier.startswith("License ::")
    )
    identifiers = set()
    for value in search_values:
        for identifier, aliases in LICENSE_ALIASES:
            if any(alias.lower() in value.lower() for alias in aliases):
                identifiers.add(identifier)
    return identifiers


def project_metadata(name: str) -> dict[str, Any]:
    with urllib.request.urlopen(PYPI_PROJECT.format(name=name), timeout=30) as response:
        return json.load(response)


def latest_upload(record: dict[str, Any]) -> datetime | None:
    version = record["info"]["version"]
    timestamps = [
        datetime.fromisoformat(file["upload_time_iso_8601"].replace("Z", "+00:00"))
        for file in record["releases"].get(version, [])
        if file.get("upload_time_iso_8601")
    ]
    return max(timestamps) if timestamps else None


def osv_advisories(packages: list[tuple[str, str]]) -> dict[str, list[str]]:
    body = json.dumps(
        {
            "queries": [
                {"package": {"ecosystem": "PyPI", "name": name}, "version": version}
                for name, version in packages
            ]
        }
    ).encode()
    request = urllib.request.Request(
        OSV_BATCH, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        results = json.load(response)["results"]
    return {
        name: sorted(vulnerability["id"] for vulnerability in result.get("vulns", []))
        for (name, _), result in zip(packages, results, strict=True)
    }


def package_policy_failures(
    name: str, license_ids: set[str], advisories: list[str], allowed_licenses: set[str]
) -> list[str]:
    failures = []
    unknown_licenses = license_ids - allowed_licenses
    if not license_ids:
        failures.append(f"dependency {name} has no recognized license")
    elif unknown_licenses:
        failures.append(f"dependency {name} has prohibited licenses: {sorted(unknown_licenses)}")
    if advisories:
        failures.append(f"dependency {name} has advisories: {advisories}")
    return failures


def audit(
    project: dict[str, Any], lock: dict[str, Any], policy: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    packages = package_index(lock)
    scopes = scoped_closures(lock)
    scope_by_package: dict[str, list[str]] = defaultdict(list)
    for scope, names in scopes.items():
        for name in names:
            scope_by_package[name].append(scope)

    import_map = policy["imports"]
    imported = imported_distributions(REPOSITORY_ROOT / "src" / "ml4t" / "live", import_map)
    failures = validate_policy_declarations(project, policy, imported)
    failures.extend(validate_locked_policy(packages, policy))
    failures.extend(
        validate_build_constraints(
            packages, scopes["build"], REPOSITORY_ROOT / "build-constraints.txt"
        )
    )
    declared_runtime = {canonical_name(name) for name in policy["dependencies"]}
    unused_direct = declared_runtime - imported
    if unused_direct:
        failures.append(f"direct runtime dependencies are not imported: {sorted(unused_direct)}")
    registry_packages = sorted(
        (
            name,
            package["version"],
        )
        for name, package in packages.items()
        if name in scope_by_package and "registry" in package.get("source", {})
    )
    advisories = osv_advisories(registry_packages)
    allowed_licenses = set(policy["allowed_license_identifiers"])
    direct = {
        canonical_name(name) for name in policy["dependencies"] | policy["build-dependencies"]
    }
    now = datetime.now(UTC)
    records = []
    for name in sorted(scope_by_package):
        package = packages[name]
        source = package.get("source", {})
        if "registry" not in source:
            policy_record = policy["dependencies"].get(name)
            license_ids = {policy_record["license"]} if policy_record else set()
            package_advisories: list[str] = []
            latest = None
            latest_version = package.get("version")
            support = policy_record["support"] if policy_record else "first-party source"
            largest_release_file_bytes = None
        else:
            metadata = project_metadata(name)
            license_ids = license_identifiers(metadata["info"])
            package_advisories = advisories[name]
            latest = latest_upload(metadata)
            latest_version = metadata["info"]["version"]
            age = (now - latest).days if latest else None
            support = (
                "active"
                if age is not None and age <= policy["abandonment_threshold_days"]
                else "mature-or-stale"
            )
            if name in direct and support != "active":
                failures.append(f"direct dependency {name} has no recent upstream release")
            release_files = metadata["releases"].get(package["version"], [])
            largest_release_file_bytes = max(
                (file.get("size", 0) for file in release_files), default=None
            )

        failures.extend(
            package_policy_failures(name, license_ids, package_advisories, allowed_licenses)
        )
        records.append(
            {
                "name": name,
                "version": package.get("version"),
                "source": "registry" if "registry" in source else "first-party-vcs",
                "scopes": sorted(scope_by_package[name]),
                "direct": name in direct,
                "directly_imported": name in imported,
                "licenses": sorted(license_ids),
                "advisories": package_advisories,
                "latest_version": latest_version,
                "latest_release_at": latest.isoformat() if latest else None,
                "support": support,
                "largest_release_file_bytes": largest_release_file_bytes,
            }
        )

    generated_at = now.isoformat()
    expires_at = (now + timedelta(days=policy["evidence_max_age_days"])).isoformat()
    report = {
        "schema_version": 1,
        "generated_at": generated_at,
        "expires_at": expires_at,
        "sources": [PYPI_PROJECT, OSV_BATCH],
        "policy": {
            "vulnerabilities": policy["vulnerability_policy"],
            "evidence_max_age_days": policy["evidence_max_age_days"],
            "abandonment_threshold_days": policy["abandonment_threshold_days"],
        },
        "scope_counts": {scope: len(names) for scope, names in scopes.items()},
        "unused_direct_dependencies": sorted(unused_direct),
        "largest_release_files": sorted(
            (
                {
                    "name": record["name"],
                    "bytes": record["largest_release_file_bytes"],
                }
                for record in records
                if record["largest_release_file_bytes"] is not None
            ),
            key=lambda item: item["bytes"],
            reverse=True,
        )[:10],
        "packages": records,
        "failures": failures,
    }
    return report, failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    project = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text())
    lock = tomllib.loads((REPOSITORY_ROOT / "uv.lock").read_text())
    policy = tomllib.loads((REPOSITORY_ROOT / "dependency-policy.toml").read_text())
    report, failures = audit(project, lock, policy)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(
        f"audited {len(report['packages'])} packages across "
        f"{report['scope_counts']}; failures={len(failures)}"
    )
    for failure in failures:
        print(f"FAIL: {failure}")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
