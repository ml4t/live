"""Qualify release security, provenance inputs, inventories, and recovery."""

from __future__ import annotations

import argparse
import hashlib
import json
import tomllib
from pathlib import Path
from typing import Any

try:
    from scripts.qualification.audit_dependencies import audit
    from scripts.qualification.build_release_inventory import build_inventories
    from scripts.qualification.check_release_recovery import POLICY_PATH, recovery_failures
    from scripts.qualification.check_workflows import validate_workflows
    from scripts.qualification.scan_release_secrets import REPOSITORY_ROOT, scan_release
except ModuleNotFoundError:
    from audit_dependencies import audit
    from build_release_inventory import build_inventories
    from check_release_recovery import POLICY_PATH, recovery_failures
    from check_workflows import validate_workflows
    from scan_release_secrets import REPOSITORY_ROOT, scan_release


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write one deterministic JSON report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def file_sha256(path: Path) -> str:
    """Return the SHA-256 digest of one retained report."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def combined_failures(reports: dict[str, dict[str, Any]]) -> list[str]:
    """Return every blocking sub-report with a bounded summary."""
    failures: list[str] = []
    for name, report in sorted(reports.items()):
        if not report.get("passed", False):
            count = len(report.get("failures", report.get("findings", [])))
            failures.append(f"{name} failed with {count} blocking findings")
    return failures


def qualify(artifacts_directory: Path, output_directory: Path) -> dict[str, Any]:
    """Run all security checks and retain their exact-artifact outputs."""
    output_directory.mkdir(parents=True, exist_ok=True)
    project = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text())
    lock = tomllib.loads((REPOSITORY_ROOT / "uv.lock").read_text())
    dependency_policy = tomllib.loads((REPOSITORY_ROOT / "dependency-policy.toml").read_text())
    dependency_report, dependency_failures = audit(project, lock, dependency_policy)
    dependency_report["passed"] = not dependency_failures
    write_json(output_directory / "dependency-audit.json", dependency_report)

    artifacts = tuple(
        path
        for path in artifacts_directory.iterdir()
        if path.suffix == ".whl" or path.name.endswith(".tar.gz")
    )
    secret_result = scan_release(REPOSITORY_ROOT, artifacts, output_directory)
    secret_report = {
        "schema_version": 1,
        "sources": secret_result.sources,
        "bytes_scanned": secret_result.bytes_scanned,
        "findings": [
            {
                "pattern": finding.pattern,
                "location_digest": finding.location_digest,
                "occurrence_count": finding.occurrence_count,
            }
            for finding in secret_result.findings
        ],
        "passed": not secret_result.findings,
    }
    write_json(output_directory / "secret-scan.json", secret_report)

    workflow_failures = validate_workflows()
    workflow_report = {
        "schema_version": 1,
        "failures": workflow_failures,
        "passed": not workflow_failures,
    }
    write_json(output_directory / "workflow-policy.json", workflow_report)

    recovery_policy = tomllib.loads(POLICY_PATH.read_text())
    recovery_policy_failures = recovery_failures(recovery_policy)
    recovery_report = {
        "schema_version": 1,
        "scenarios": sorted(recovery_policy.get("scenarios", {})),
        "failures": recovery_policy_failures,
        "passed": not recovery_policy_failures,
    }
    write_json(output_directory / "release-recovery.json", recovery_report)

    snapshot, sbom = build_inventories(artifacts_directory)
    write_json(output_directory / "dependency-snapshot.json", snapshot)
    write_json(output_directory / "sbom.cdx.json", sbom)

    reports = {
        "dependency-audit": dependency_report,
        "secret-scan": secret_report,
        "workflow-policy": workflow_report,
        "release-recovery": recovery_report,
    }
    failures = combined_failures(reports)
    retained = {
        path.name: file_sha256(path)
        for path in sorted(output_directory.glob("*.json"))
        if path.name != "security-report.json"
    }
    report = {
        "schema_version": 1,
        "commit": snapshot["commit"],
        "version": snapshot["version"],
        "artifacts": snapshot["artifacts"],
        "controls": {
            "known_advisories": dependency_report["policy"]["vulnerabilities"],
            "secret_scan_includes_history": True,
            "actions_pinned": not workflow_failures,
            "least_privilege_checked": not workflow_failures,
            "trusted_publication_checked": not workflow_failures,
            "attestations_checked": not workflow_failures,
            "cyclonedx_version": sbom["specVersion"],
            "immutable_recovery_scenarios": recovery_report["scenarios"],
        },
        "retained_reports": retained,
        "failures": failures,
        "passed": not failures,
    }
    write_json(output_directory / "security-report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts-dir", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    report = qualify(args.artifacts_dir.resolve(), args.output_directory.resolve())
    print(
        f"stable security qualification: {'PASS' if report['passed'] else 'FAIL'} "
        f"({len(report['retained_reports'])} retained reports)"
    )
    for failure in report["failures"]:
        print(f"- {failure}")
    return int(not report["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
