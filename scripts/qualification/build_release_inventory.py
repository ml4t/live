"""Build deterministic dependency and CycloneDX inventories for exact artifacts."""

from __future__ import annotations

import argparse
import email
import hashlib
import json
import subprocess
import tomllib
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.qualification.audit_dependencies import canonical_name, package_index, scoped_closures
from scripts.qualification.qualify_artifacts import distribution_pair

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def sha256(path: Path) -> str:
    """Return the SHA-256 digest for one artifact."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def wheel_version(path: Path) -> str:
    """Read the project version from wheel metadata."""
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(names) != 1:
            raise ValueError("wheel must contain exactly one METADATA file")
        metadata = email.message_from_bytes(archive.read(names[0]))
    version = metadata.get("Version")
    if not version:
        raise ValueError("wheel metadata has no Version")
    return version


def git_output(*arguments: str) -> str:
    """Return one git value for the source revision."""
    return subprocess.run(
        ["git", *arguments],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def package_source(package: dict[str, Any]) -> str:
    """Return a non-secret source class for one lock record."""
    source = package.get("source", {})
    if "registry" in source:
        return "registry"
    if "git" in source:
        return "git"
    if "editable" in source or "virtual" in source:
        return "project"
    return "unknown"


def dependency_records(lock: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, set[str]]]:
    """Return locked release records and their declared qualification scopes."""
    packages = package_index(lock)
    scopes = scoped_closures(lock)
    selected = set().union(*scopes.values())
    records: list[dict[str, Any]] = []
    for name in sorted(selected):
        package = packages[name]
        records.append(
            {
                "name": name,
                "version": str(package["version"]),
                "source": package_source(package),
                "scopes": sorted(scope for scope, members in scopes.items() if name in members),
                "dependencies": sorted(
                    canonical_name(dependency["name"])
                    for dependency in package.get("dependencies", [])
                    if canonical_name(dependency["name"]) in selected
                ),
            }
        )
    return records, scopes


def build_inventories(
    artifacts_directory: Path,
    *,
    commit: str | None = None,
    commit_timestamp: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the dependency snapshot and CycloneDX document."""
    wheel, sdist = distribution_pair(artifacts_directory)
    version = wheel_version(wheel)
    commit = commit or git_output("rev-parse", "HEAD")
    commit_timestamp = commit_timestamp or git_output("show", "-s", "--format=%cI", commit)
    project = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text())
    lock = tomllib.loads((REPOSITORY_ROOT / "uv.lock").read_text())
    records, scopes = dependency_records(lock)
    artifact_records = [{"filename": path.name, "sha256": sha256(path)} for path in (wheel, sdist)]
    snapshot = {
        "schema_version": 1,
        "project": "ml4t-live",
        "version": version,
        "commit": commit,
        "commit_timestamp": commit_timestamp,
        "artifacts": artifact_records,
        "direct_requirements": sorted(project["project"]["dependencies"]),
        "optional_requirements": {
            name: sorted(requirements)
            for name, requirements in sorted(
                project["project"].get("optional-dependencies", {}).items()
            )
        },
        "build_requirements": sorted(project["build-system"]["requires"]),
        "scope_counts": {name: len(members) for name, members in sorted(scopes.items())},
        "packages": records,
    }

    root_ref = f"pkg:pypi/ml4t-live@{version}"
    components = [
        {
            "type": "library",
            "bom-ref": f"pkg:pypi/{record['name']}@{record['version']}",
            "name": record["name"],
            "version": record["version"],
            "purl": f"pkg:pypi/{record['name']}@{record['version']}",
            "scope": "required" if "runtime" in record["scopes"] else "optional",
            "properties": [
                {"name": "ml4t:qualification:scope", "value": scope} for scope in record["scopes"]
            ],
        }
        for record in records
    ]
    direct_names = {
        canonical_name(
            requirement.split("[", 1)[0].split("<", 1)[0].split(">", 1)[0].split("=", 1)[0]
        )
        for requirement in project["project"]["dependencies"]
    }
    dependencies = [
        {
            "ref": root_ref,
            "dependsOn": sorted(
                f"pkg:pypi/{record['name']}@{record['version']}"
                for record in records
                if record["name"] in direct_names
            ),
        }
    ]
    versions = {record["name"]: record["version"] for record in records}
    dependencies.extend(
        {
            "ref": f"pkg:pypi/{record['name']}@{record['version']}",
            "dependsOn": sorted(
                f"pkg:pypi/{name}@{versions[name]}"
                for name in record["dependencies"]
                if name in versions
            ),
        }
        for record in records
    )
    serial_seed = f"{commit}:{artifact_records[0]['sha256']}:{artifact_records[1]['sha256']}"
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, serial_seed)}",
        "version": 1,
        "metadata": {
            "timestamp": datetime.fromisoformat(commit_timestamp).astimezone(UTC).isoformat(),
            "component": {
                "type": "library",
                "bom-ref": root_ref,
                "name": "ml4t-live",
                "version": version,
                "purl": root_ref,
                "properties": [
                    {"name": "ml4t:source:commit", "value": commit},
                    *(
                        {
                            "name": f"ml4t:artifact:{record['filename']}:sha256",
                            "value": record["sha256"],
                        }
                        for record in artifact_records
                    ),
                ],
            },
        },
        "components": components,
        "dependencies": dependencies,
    }
    return snapshot, sbom


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts-dir", type=Path, required=True)
    parser.add_argument("--snapshot-output", type=Path, required=True)
    parser.add_argument("--sbom-output", type=Path, required=True)
    args = parser.parse_args()

    snapshot, sbom = build_inventories(args.artifacts_dir.resolve())
    for path, payload in ((args.snapshot_output, snapshot), (args.sbom_output, sbom)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(
        f"release inventory: PASS ({len(snapshot['packages'])} locked packages, "
        f"{len(snapshot['artifacts'])} artifacts)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
