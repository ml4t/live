"""Verify stable release identity before any publication write."""

from __future__ import annotations

import argparse
import email
import hashlib
import json
import re
import zipfile
from pathlib import Path
from typing import Any

COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
STABLE_VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:\.post[0-9]+)?")
HASH_PATTERN = re.compile(r"[0-9a-f]{64}")


def sha256(path: Path) -> str:
    """Return the SHA-256 digest for one file."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    """Load one JSON object."""
    loaded = json.loads(path.read_text())
    if not isinstance(loaded, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return loaded


def artifact_identity(directory: Path) -> tuple[str, dict[str, dict[str, str]]]:
    """Return version and exact identity for one wheel and source distribution."""
    wheels = sorted(directory.glob("*.whl"))
    sdists = sorted(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("release input must contain exactly one wheel and one source distribution")
    with zipfile.ZipFile(wheels[0]) as archive:
        names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(names) != 1:
            raise ValueError("release wheel has no unique METADATA file")
        version = email.message_from_bytes(archive.read(names[0])).get("Version")
    if not version:
        raise ValueError("release wheel has no version")
    return version, {
        "wheel": {"filename": wheels[0].name, "sha256": sha256(wheels[0])},
        "sdist": {"filename": sdists[0].name, "sha256": sha256(sdists[0])},
    }


def list_artifacts(records: Any) -> dict[str, dict[str, str]]:
    """Index list-shaped artifact records by distribution type."""
    if not isinstance(records, list):
        return {}
    indexed: dict[str, dict[str, str]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        filename = str(record.get("filename", ""))
        kind = (
            "wheel"
            if filename.endswith(".whl")
            else "sdist"
            if filename.endswith(".tar.gz")
            else ""
        )
        if kind:
            indexed[kind] = {"filename": filename, "sha256": str(record.get("sha256", ""))}
    return indexed


def identity_failures(
    *,
    artifacts_directory: Path,
    artifact_report: dict[str, Any],
    security_report: dict[str, Any],
    sbom: dict[str, Any],
    dependency_snapshot: dict[str, Any],
    commit: str,
    tag: str,
    paper_wheel_sha256: str,
    paper_sdist_sha256: str,
    sbom_sha256: str,
    dependency_snapshot_sha256: str,
) -> list[str]:
    """Return every identity mismatch before publication."""
    failures: list[str] = []
    try:
        version, artifacts = artifact_identity(artifacts_directory)
    except (ValueError, zipfile.BadZipFile) as error:
        return [str(error)]
    if not COMMIT_PATTERN.fullmatch(commit):
        failures.append("release commit is not a full lowercase hash")
    if not STABLE_VERSION_PATTERN.fullmatch(version):
        failures.append("release version is not stable")
    if tag != f"v{version}":
        failures.append("release tag does not equal the artifact version")
    for kind, paper_hash in (
        ("wheel", paper_wheel_sha256),
        ("sdist", paper_sdist_sha256),
    ):
        if not HASH_PATTERN.fullmatch(paper_hash) or paper_hash != artifacts[kind]["sha256"]:
            failures.append(f"paper evidence {kind} hash differs from the release artifact")

    expected_artifact_report = {
        "wheel": artifacts["wheel"],
        "sdist": artifacts["sdist"],
    }
    if (
        artifact_report.get("passed") is not True
        or artifact_report.get("commit") != commit
        or artifact_report.get("version") != version
        or artifact_report.get("dirty_candidate") is not False
        or artifact_report.get("reproducible") is not True
        or artifact_report.get("manifests_exact") is not True
        or artifact_report.get("artifacts") != expected_artifact_report
        or not all(profile.get("passed") is True for profile in artifact_report.get("profiles", []))
    ):
        failures.append("artifact qualification identity differs from the release")

    if (
        security_report.get("passed") is not True
        or security_report.get("commit") != commit
        or security_report.get("version") != version
        or list_artifacts(security_report.get("artifacts")) != artifacts
        or security_report.get("retained_reports", {}).get("sbom.cdx.json") != sbom_sha256
        or security_report.get("retained_reports", {}).get("dependency-snapshot.json")
        != dependency_snapshot_sha256
    ):
        failures.append("security qualification identity differs from the release")

    if (
        dependency_snapshot.get("commit") != commit
        or dependency_snapshot.get("version") != version
        or list_artifacts(dependency_snapshot.get("artifacts")) != artifacts
    ):
        failures.append("dependency snapshot identity differs from the release")

    component = sbom.get("metadata", {}).get("component", {})
    properties = {
        item.get("name"): item.get("value")
        for item in component.get("properties", [])
        if isinstance(item, dict)
    }
    expected_properties = {
        "ml4t:source:commit": commit,
        **{
            f"ml4t:artifact:{record['filename']}:sha256": record["sha256"]
            for record in artifacts.values()
        },
    }
    if (
        sbom.get("bomFormat") != "CycloneDX"
        or sbom.get("specVersion") != "1.6"
        or component.get("version") != version
        or not expected_properties.items() <= properties.items()
    ):
        failures.append("SBOM identity differs from the release")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts-dir", type=Path, required=True)
    parser.add_argument("--artifact-report", type=Path, required=True)
    parser.add_argument("--security-report", type=Path, required=True)
    parser.add_argument("--sbom", type=Path, required=True)
    parser.add_argument("--dependency-snapshot", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--paper-wheel-sha256", required=True)
    parser.add_argument("--paper-sdist-sha256", required=True)
    args = parser.parse_args()
    failures = identity_failures(
        artifacts_directory=args.artifacts_dir,
        artifact_report=load_json(args.artifact_report),
        security_report=load_json(args.security_report),
        sbom=load_json(args.sbom),
        dependency_snapshot=load_json(args.dependency_snapshot),
        commit=args.commit,
        tag=args.tag,
        paper_wheel_sha256=args.paper_wheel_sha256,
        paper_sdist_sha256=args.paper_sdist_sha256,
        sbom_sha256=sha256(args.sbom),
        dependency_snapshot_sha256=sha256(args.dependency_snapshot),
    )
    print(f"stable release identity: {'PASS' if not failures else 'FAIL'}")
    for failure in failures:
        print(f"- {failure}")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
