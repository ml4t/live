"""Validate workflow permissions, immutable actions, and promotion dependencies."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_ROOT = REPOSITORY_ROOT / ".github" / "workflows"
SUPPORTED_PYTHONS = {"3.12", "3.13", "3.14"}
MANDATORY_JOBS = {
    "source-quality",
    "deterministic",
    "dependency",
    "stress",
    "performance",
    "documentation",
}
ACTION_PATTERN = re.compile(
    r"^\s*-?\s*uses:\s*([^\s#]+)(?:\s+#\s+(v[0-9][A-Za-z0-9_.-]*))?\s*$",
    re.MULTILINE,
)
IMMUTABLE_ACTION = re.compile(r"^[^/\s]+/[^/@\s]+@[0-9a-f]{40}$")


def load_workflow(path: Path) -> dict[str, Any]:
    loaded = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    if not isinstance(loaded, dict):
        raise ValueError(f"workflow is not a mapping: {path}")
    return loaded


def _list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _needs(job: dict[str, Any]) -> set[str]:
    return set(_list(job.get("needs")))


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job.get("steps", [])
    return [step for step in steps if isinstance(step, dict)] if isinstance(steps, list) else []


def _run_text(job: dict[str, Any]) -> str:
    return "\n".join(str(step.get("run", "")) for step in _steps(job))


def _uses(job: dict[str, Any]) -> list[str]:
    values = [str(job["uses"])] if "uses" in job else []
    values.extend(str(step["uses"]) for step in _steps(job) if "uses" in step)
    return values


def _action_steps(job: dict[str, Any], owner_and_name: str) -> list[dict[str, Any]]:
    prefix = f"{owner_and_name}@"
    return [step for step in _steps(job) if str(step.get("uses", "")).startswith(prefix)]


def _triggers(workflow: dict[str, Any]) -> set[str]:
    trigger = workflow.get("on")
    if isinstance(trigger, dict):
        return {str(key) for key in trigger}
    return set(_list(trigger))


def _matrix_pythons(job: dict[str, Any]) -> set[str]:
    strategy = job.get("strategy", {})
    matrix = strategy.get("matrix", {}) if isinstance(strategy, dict) else {}
    return set(_list(matrix.get("python-version"))) if isinstance(matrix, dict) else set()


def _permission_failures(label: str, permissions: Any, allowed_writes: set[str]) -> list[str]:
    if permissions is None:
        return [f"{label} has implicit permissions"]
    if not isinstance(permissions, dict):
        return [f"{label} permissions must be an explicit mapping"]
    failures = []
    for name, access in permissions.items():
        if access == "write" and name not in allowed_writes:
            failures.append(f"{label} grants unexpected {name}:write")
        elif access not in {"read", "write", "none"}:
            failures.append(f"{label} has invalid {name} permission {access}")
    return failures


def action_pin_failures(paths: Iterable[Path]) -> list[str]:
    failures = []
    for path in paths:
        for action, version in ACTION_PATTERN.findall(path.read_text()):
            if action.startswith("./"):
                continue
            if not IMMUTABLE_ACTION.fullmatch(action):
                failures.append(f"{path.name} action is not pinned: {action}")
            if not version:
                failures.append(f"{path.name} pinned action lacks update comment: {action}")
    return failures


def promotion_failures(qualification: dict[str, Any], release: dict[str, Any]) -> list[str]:
    failures = []
    jobs = qualification.get("jobs", {})
    if not isinstance(jobs, dict):
        return ["qualification workflow has no jobs mapping"]
    missing = MANDATORY_JOBS - jobs.keys()
    if missing:
        failures.append(f"qualification workflow lacks mandatory jobs: {sorted(missing)}")

    build = jobs.get("build", {})
    artifact = jobs.get("artifact-qualification", {})
    if _needs(build) != MANDATORY_JOBS:
        failures.append("build does not require every mandatory qualification job")
    if _needs(artifact) != {"build"}:
        failures.append("artifact qualification does not require the candidate build")
    for name in ("build", "artifact-qualification"):
        if "if" in jobs.get(name, {}):
            failures.append(f"{name} overrides default success dependency semantics")

    release_jobs = release.get("jobs", {})
    if not isinstance(release_jobs, dict):
        return [*failures, "release workflow has no jobs mapping"]
    if release_jobs.get("qualification", {}).get("uses") != (
        "./.github/workflows/qualification.yml"
    ):
        failures.append("release does not call the reusable qualification workflow")
    if _needs(release_jobs.get("publish", {})) != {"qualification", "paper-evidence"}:
        failures.append(
            "publish does not require the complete reusable qualification and fresh paper evidence"
        )
    if _needs(release_jobs.get("github-release", {})) != {"publish"}:
        failures.append("GitHub release does not require successful publication")
    for name in ("publish", "github-release"):
        if "if" in release_jobs.get(name, {}):
            failures.append(f"{name} overrides default success dependency semantics")
    if "uv build" in _run_text(release_jobs.get("publish", {})):
        failures.append("publish rebuilds the candidate")
    paper_evidence = release_jobs.get("paper-evidence", {})
    if "check_paper_evidence.py" not in _run_text(paper_evidence):
        failures.append("release does not verify fresh paper evidence for the exact commit")
    if paper_evidence.get("outputs", {}).get("wheel_sha256") != (
        "${{ steps.paper.outputs.wheel_sha256 }}"
    ):
        failures.append("release does not expose the paper-qualified wheel hash")
    publish_text = json.dumps(release_jobs.get("publish", {}), sort_keys=True)
    if (
        "needs.paper-evidence.outputs.wheel_sha256" not in publish_text
        or "sha256sum --check" not in publish_text
    ):
        failures.append("publish does not match its wheel to the paper-qualified hash")
    return failures


def validate_workflows(root: Path = WORKFLOW_ROOT) -> list[str]:
    paths = sorted(root.glob("*.yml"))
    workflows = {path.name: load_workflow(path) for path in paths}
    required = {"ci.yml", "qualification.yml", "release.yml", "paper.yml", "docs.yml"}
    failures = []
    if missing := required - workflows.keys():
        failures.append(f"missing workflows: {sorted(missing)}")
        return failures
    failures.extend(action_pin_failures(paths))

    ci = workflows["ci.yml"]
    qualification = workflows["qualification.yml"]
    release = workflows["release.yml"]
    paper = workflows["paper.yml"]
    for name, workflow in workflows.items():
        failures.extend(_permission_failures(name, workflow.get("permissions"), set()))
        if "pull_request_target" in _triggers(workflow):
            failures.append(f"{name} executes pull_request_target code")

    qualification_jobs = qualification["jobs"]
    for name in ("source-quality", "deterministic"):
        if _matrix_pythons(qualification_jobs.get(name, {})) != SUPPORTED_PYTHONS:
            failures.append(f"{name} does not cover the supported Python matrix")
        if "uv sync --python ${{ matrix.python-version }}" not in _run_text(
            qualification_jobs.get(name, {})
        ):
            failures.append(f"{name} does not select its matrix interpreter")
    if "--group source" not in _run_text(qualification_jobs.get("source-quality", {})):
        failures.append("source-quality does not call the authoritative source group")
    if "--group deterministic" not in _run_text(qualification_jobs.get("deterministic", {})):
        failures.append("deterministic does not call the authoritative deterministic group")
    if "--group dependency" not in _run_text(qualification_jobs.get("dependency", {})):
        failures.append("dependency does not call the authoritative dependency group")
    if "--group stress" not in _run_text(qualification_jobs.get("stress", {})):
        failures.append("stress does not call the authoritative stress group")
    if "--group performance" not in _run_text(qualification_jobs.get("performance", {})):
        failures.append("performance does not call the authoritative performance group")
    if "--group documentation" not in _run_text(qualification_jobs.get("documentation", {})):
        failures.append("documentation does not call the authoritative documentation group")
    if "--artifacts-dir dist" not in _run_text(
        qualification_jobs.get("artifact-qualification", {})
    ):
        failures.append("the uploaded artifact is not passed to artifact qualification")
    build_job = qualification_jobs.get("build", {})
    artifact_job = qualification_jobs.get("artifact-qualification", {})
    if "SOURCE_DATE_EPOCH" not in _run_text(build_job):
        failures.append("candidate build does not fix its reproducible timestamp")
    build_uploads = _action_steps(build_job, "actions/upload-artifact")
    artifact_downloads = _action_steps(artifact_job, "actions/download-artifact")
    expected_artifact_name = "dist-${{ github.sha }}"
    if len(build_uploads) != 1 or build_uploads[0].get("with", {}).get("name") != (
        expected_artifact_name
    ):
        failures.append("candidate build does not upload one commit-addressed artifact")
    if len(artifact_downloads) != 1 or artifact_downloads[0].get("with", {}).get("name") != (
        expected_artifact_name
    ):
        failures.append("artifact qualification does not download the candidate artifact")

    reusable_text = json.dumps(qualification, sort_keys=True)
    ci_text = json.dumps(ci, sort_keys=True)
    if "secrets." in reusable_text or "secrets." in ci_text:
        failures.append("pull-request qualification references a secret")
    if any("environment" in job for job in qualification_jobs.values()):
        failures.append("pull-request qualification uses a protected credential environment")
    if ci.get("jobs", {}).get("qualification", {}).get("uses") != (
        "./.github/workflows/qualification.yml"
    ):
        failures.append("CI does not call the reusable qualification workflow")

    if _triggers(release) != {"push"}:
        failures.append("release has a non-tag trigger")
    release_push = release.get("on", {}).get("push", {})
    if not isinstance(release_push, dict) or _list(release_push.get("tags")) != ["v*"]:
        failures.append("release is not restricted to version tags")
    failures.extend(promotion_failures(qualification, release))
    publish_job = release.get("jobs", {}).get("publish", {})
    publish_downloads = _action_steps(publish_job, "actions/download-artifact")
    if len(publish_downloads) != 1 or publish_downloads[0].get("with", {}).get("name") != (
        expected_artifact_name
    ):
        failures.append("publish does not download the qualified candidate artifact")
    publishers = _action_steps(publish_job, "pypa/gh-action-pypi-publish")
    if len(publishers) != 1 or publishers[0].get("with", {}).get("attestations") != "true":
        failures.append("publish does not preserve trusted provenance attestations")

    if _triggers(paper) != {"workflow_dispatch"}:
        failures.append("paper qualification is not manual-only")
    paper_job = paper.get("jobs", {}).get("paper", {})
    if paper_job.get("environment") != "paper":
        failures.append("paper qualification does not use the protected paper environment")
    if "secrets." not in json.dumps(paper_job, sort_keys=True):
        failures.append("paper qualification has no explicit protected credential inputs")
    paper_run_text = _run_text(paper_job)
    if "qualify_paper.py candidate" not in paper_run_text:
        failures.append("paper qualification does not bind the downloaded candidate artifact")
    for provider in ("alpaca", "ib"):
        for phase in ("exercise", "restart"):
            if (
                f"--provider {provider}" not in paper_run_text
                or f"--phase {phase}" not in paper_run_text
            ):
                failures.append(f"paper qualification lacks {provider} {phase} evidence")
    if "qualify_paper.py assemble" not in paper_run_text:
        failures.append("paper qualification does not require a complete redacted evidence bundle")
    if "uv sync" in paper_run_text or "uv pip install" not in paper_run_text:
        failures.append("paper qualification does not install only the candidate wheel")
    checkouts = _action_steps(paper_job, "actions/checkout")
    if len(checkouts) != 1 or checkouts[0].get("with", {}).get("ref") != (
        "${{ inputs.candidate-sha }}"
    ):
        failures.append("paper qualification is not tied to its requested candidate commit")
    paper_downloads = _action_steps(paper_job, "actions/download-artifact")
    if len(paper_downloads) != 1:
        failures.append("paper qualification does not download one candidate artifact")
    else:
        download_inputs = paper_downloads[0].get("with", {})
        if download_inputs.get("name") != "dist-${{ inputs.candidate-sha }}":
            failures.append("paper qualification downloads an artifact for a different commit")
        if download_inputs.get("run-id") != "${{ inputs.qualification-run-id }}":
            failures.append("paper qualification is not tied to a successful qualification run")
    uploads = _action_steps(paper_job, "actions/upload-artifact")
    if len(uploads) != 1 or uploads[0].get("with", {}).get("path") != "paper-evidence/":
        failures.append("paper qualification does not retain its evidence bundle")

    allowed_job_writes = {
        ("release.yml", "publish"): {"id-token"},
        ("release.yml", "github-release"): {"contents"},
    }
    for workflow_name, workflow in workflows.items():
        for job_name, job in workflow.get("jobs", {}).items():
            failures.extend(
                _permission_failures(
                    f"{workflow_name}:{job_name}",
                    job.get("permissions"),
                    allowed_job_writes.get((workflow_name, job_name), set()),
                )
            )

    dependabot = REPOSITORY_ROOT / ".github" / "dependabot.yml"
    if 'package-ecosystem: "github-actions"' not in dependabot.read_text():
        failures.append("Dependabot does not maintain pinned GitHub Actions")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    failures = validate_workflows()
    report = {
        "schema_version": 1,
        "workflow_root": str(WORKFLOW_ROOT),
        "failures": failures,
        "passed": not failures,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"workflow policy: {'PASS' if not failures else 'FAIL'} ({len(failures)} failures)")
    for failure in failures:
        print(f"- {failure}")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
