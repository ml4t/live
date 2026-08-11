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
POST_BUILD_JOBS = {"artifact-qualification", "security"}
PAPER_QUICK_STEP_IDS = (
    "alpaca-exercise",
    "alpaca-restart",
    "ib-exercise",
    "ib-restart",
    "okx-external",
)
ACTION_PATTERN = re.compile(
    r"^\s*-?\s*uses:\s*([^\s#]+)(?:\s+#\s+(v[0-9][A-Za-z0-9_.-]*))?\s*$",
    re.MULTILINE,
)
IMMUTABLE_ACTION = re.compile(r"^[^/\s]+/[^/@\s]+(?:/[^@\s]+)*@[0-9a-f]{40}$")


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
    security = jobs.get("security", {})
    if _needs(build) != MANDATORY_JOBS:
        failures.append("build does not require every mandatory qualification job")
    if _needs(artifact) != {"build"}:
        failures.append("artifact qualification does not require the candidate build")
    if _needs(security) != {"build"}:
        failures.append("security qualification does not require the candidate build")
    for name in ("build", *sorted(POST_BUILD_JOBS)):
        if "if" in jobs.get(name, {}):
            failures.append(f"{name} overrides default success dependency semantics")

    release_jobs = release.get("jobs", {})
    if not isinstance(release_jobs, dict):
        return [*failures, "release workflow has no jobs mapping"]
    if release_jobs.get("qualification", {}).get("uses") != (
        "./.github/workflows/stable-qualification.yml"
    ):
        failures.append("release does not call the reusable stable qualification workflow")
    if _needs(release_jobs.get("publish", {})) != {
        "ecosystem-qualification",
        "qualification",
        "paper-evidence",
    }:
        failures.append(
            "publish does not require the complete stable qualification and fresh paper evidence"
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
    if paper_evidence.get("outputs", {}).get("sdist_sha256") != (
        "${{ steps.paper.outputs.sdist_sha256 }}"
    ):
        failures.append("release does not expose the paper-qualified source-distribution hash")
    publish_text = json.dumps(release_jobs.get("publish", {}), sort_keys=True)
    for required in (
        "needs.paper-evidence.outputs.wheel_sha256",
        "needs.paper-evidence.outputs.sdist_sha256",
        "verify_release_identity.py",
        "security-evidence/sbom.cdx.json",
        "security-evidence/dependency-snapshot.json",
    ):
        if required not in publish_text:
            failures.append(f"publish identity check omits: {required}")
    return failures


def release_recovery_failures(release: dict[str, Any]) -> list[str]:
    """Reject recovery paths that can publish without the original evidence."""
    failures = []
    recovery_publish = release.get("jobs", {}).get("recovery-publish", {})
    if _needs(recovery_publish) != {"paper-evidence"}:
        failures.append("recovery publish does not require fresh paper evidence")
    if recovery_publish.get("if") != "github.event_name == 'workflow_dispatch'":
        failures.append("recovery publish is not restricted to manual dispatch")
    recovery_downloads = _action_steps(recovery_publish, "actions/download-artifact")
    expected_recovery_downloads = {
        "dist-${{ inputs.candidate-sha }}",
        "qualification-${{ inputs.candidate-sha }}",
        "security-${{ inputs.candidate-sha }}",
    }
    if {step.get("with", {}).get("name") for step in recovery_downloads} != (
        expected_recovery_downloads
    ) or any(
        step.get("with", {}).get("run-id") != "${{ inputs.qualification-run-id }}"
        for step in recovery_downloads
    ):
        failures.append("recovery publish does not use every exact source-run artifact")
    recovery_text = json.dumps(recovery_publish, sort_keys=True)
    for required in (
        "needs.paper-evidence.outputs.wheel_sha256",
        "needs.paper-evidence.outputs.sdist_sha256",
        "verify_release_identity.py",
        "inputs.candidate-sha",
        "inputs.tag",
    ):
        if required not in recovery_text:
            failures.append(f"recovery identity check omits: {required}")
    recovery_publishers = _action_steps(recovery_publish, "pypa/gh-action-pypi-publish")
    if (
        len(recovery_publishers) != 1
        or recovery_publishers[0].get("with", {}).get("attestations") != "true"
    ):
        failures.append("recovery publish does not preserve trusted provenance attestations")
    recovery_release = release.get("jobs", {}).get("recovery-github-release", {})
    if _needs(recovery_release) != {"recovery-publish"}:
        failures.append("recovered GitHub release does not require successful publication")
    if recovery_release.get("if") != "github.event_name == 'workflow_dispatch'":
        failures.append("recovered GitHub release is not restricted to manual dispatch")
    recovery_release_text = _run_text(recovery_release)
    if "sbom.cdx.json" not in recovery_release_text or "dependency-snapshot.json" not in (
        recovery_release_text
    ):
        failures.append("recovered GitHub release does not retain security evidence")
    if '--repo "${{ github.repository }}"' not in recovery_release_text:
        failures.append("recovered GitHub release does not address its repository explicitly")
    return failures


def paper_soak_failures(paper_job: dict[str, Any]) -> list[str]:
    """Reject a long soak that can start after a short provider check fails."""
    soak_steps = [step for step in _steps(paper_job) if step.get("id") == "provider-soaks"]
    if len(soak_steps) != 1:
        return ["paper qualification must define one provider-soaks step"]
    condition = str(soak_steps[0].get("if", ""))
    failures = []
    for step_id in PAPER_QUICK_STEP_IDS:
        required = f"steps.{step_id}.outcome == 'success'"
        if required not in condition:
            failures.append(f"paper soak does not require successful {step_id}")
    return failures


def validate_workflows(root: Path = WORKFLOW_ROOT) -> list[str]:
    paths = sorted(root.glob("*.yml"))
    workflows = {path.name: load_workflow(path) for path in paths}
    required = {
        "ci.yml",
        "stable-qualification.yml",
        "release.yml",
        "paper.yml",
        "docs.yml",
    }
    failures = []
    if missing := required - workflows.keys():
        failures.append(f"missing workflows: {sorted(missing)}")
        return failures
    failures.extend(action_pin_failures(paths))

    ci = workflows["ci.yml"]
    qualification = workflows["stable-qualification.yml"]
    release = workflows["release.yml"]
    paper = workflows["paper.yml"]
    for name, workflow in workflows.items():
        failures.extend(_permission_failures(name, workflow.get("permissions"), set()))
        if "pull_request_target" in _triggers(workflow):
            failures.append(f"{name} executes pull_request_target code")

    qualification_jobs = qualification["jobs"]
    if qualification.get("env", {}).get("SETUPTOOLS_SCM_PRETEND_VERSION") != "0.1.1":
        failures.append("stable qualification does not build the exact 0.1.1 candidate version")
    if qualification.get("env", {}).get("CANDIDATE_SHA") != (
        "${{ github.event.pull_request.head.sha || github.sha }}"
    ):
        failures.append("qualification does not resolve the pull-request head candidate")
    for name, job in qualification_jobs.items():
        checkouts = _action_steps(job, "actions/checkout")
        if len(checkouts) != 1 or checkouts[0].get("with", {}).get("ref") != (
            "${{ env.CANDIDATE_SHA }}"
        ):
            failures.append(f"{name} does not check out the exact candidate commit")
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
    security_job = qualification_jobs.get("security", {})
    security_text = _run_text(security_job)
    if "qualify_security.py" not in security_text or "--artifacts-dir dist" not in security_text:
        failures.append("security qualification does not inspect the exact candidate artifacts")
    build_job = qualification_jobs.get("build", {})
    artifact_job = qualification_jobs.get("artifact-qualification", {})
    if "SOURCE_DATE_EPOCH" not in _run_text(build_job):
        failures.append("candidate build does not fix its reproducible timestamp")
    build_uploads = _action_steps(build_job, "actions/upload-artifact")
    artifact_downloads = _action_steps(artifact_job, "actions/download-artifact")
    expected_artifact_name = "dist-${{ env.CANDIDATE_SHA }}"
    if len(build_uploads) != 1 or build_uploads[0].get("with", {}).get("name") != (
        expected_artifact_name
    ):
        failures.append("candidate build does not upload one commit-addressed artifact")
    if len(artifact_downloads) != 1 or artifact_downloads[0].get("with", {}).get("name") != (
        expected_artifact_name
    ):
        failures.append("artifact qualification does not download the candidate artifact")
    performance_uploads = _action_steps(
        qualification_jobs.get("performance", {}), "actions/upload-artifact"
    )
    if len(performance_uploads) != 1 or performance_uploads[0].get("with", {}).get("name") != (
        "performance-${{ env.CANDIDATE_SHA }}"
    ):
        failures.append("performance evidence is not addressed by the candidate commit")
    qualification_uploads = _action_steps(artifact_job, "actions/upload-artifact")
    if (
        len(qualification_uploads) != 1
        or qualification_uploads[0].get("with", {}).get("name")
        != "qualification-${{ env.CANDIDATE_SHA }}"
    ):
        failures.append("artifact qualification evidence is not addressed by the candidate commit")
    security_uploads = _action_steps(security_job, "actions/upload-artifact")
    if len(security_uploads) != 1 or security_uploads[0].get("with", {}).get("name") != (
        "security-${{ env.CANDIDATE_SHA }}"
    ):
        failures.append("security evidence is not addressed by the candidate commit")

    reusable_text = json.dumps(qualification, sort_keys=True)
    ci_text = json.dumps(ci, sort_keys=True)
    if "secrets." in reusable_text or "secrets." in ci_text:
        failures.append("pull-request qualification references a secret")
    if any("environment" in job for job in qualification_jobs.values()):
        failures.append("pull-request qualification uses a protected credential environment")
    if ci.get("jobs", {}).get("qualification", {}).get("uses") != (
        "./.github/workflows/stable-qualification.yml"
    ):
        failures.append("CI does not call the reusable qualification workflow")

    if _triggers(release) != {"push", "workflow_dispatch"}:
        failures.append("release triggers differ from tag publication and manual recovery")
    release_push = release.get("on", {}).get("push", {})
    if not isinstance(release_push, dict) or _list(release_push.get("tags")) != ["v*"]:
        failures.append("release is not restricted to version tags")
    recovery_inputs = release.get("on", {}).get("workflow_dispatch", {}).get("inputs", {})
    if set(recovery_inputs) != {"candidate-sha", "qualification-run-id", "tag"} or any(
        value.get("required") != "true" for value in recovery_inputs.values()
    ):
        failures.append("release recovery does not require the candidate, source run, and tag")
    failures.extend(promotion_failures(qualification, release))
    publish_job = release.get("jobs", {}).get("publish", {})
    publish_download_names = {
        step.get("with", {}).get("name")
        for step in _action_steps(publish_job, "actions/download-artifact")
    }
    expected_publish_downloads = {
        "dist-${{ github.sha }}",
        "qualification-${{ github.sha }}",
        "security-${{ github.sha }}",
    }
    if publish_download_names != expected_publish_downloads:
        failures.append("publish does not download every qualified identity artifact")
    publishers = _action_steps(publish_job, "pypa/gh-action-pypi-publish")
    if len(publishers) != 1 or publishers[0].get("with", {}).get("attestations") != "true":
        failures.append("publish does not preserve trusted provenance attestations")
    release_text = _run_text(release.get("jobs", {}).get("github-release", {}))
    if "sbom.cdx.json" not in release_text or "dependency-snapshot.json" not in release_text:
        failures.append("GitHub release does not retain the SBOM and dependency snapshot")
    if '--repo "${{ github.repository }}"' not in release_text:
        failures.append("GitHub release does not address its repository explicitly")

    failures.extend(release_recovery_failures(release))

    stable_workflow_text = json.dumps(qualification, sort_keys=True).casefold()
    if "0.1.0b" in stable_workflow_text or "beta" in stable_workflow_text:
        failures.append("stable qualification contains a beta version or maturity assumption")

    if _triggers(paper) != {"workflow_dispatch"}:
        failures.append("paper qualification is not manual-only")
    paper_job = paper.get("jobs", {}).get("paper", {})
    failures.extend(paper_soak_failures(paper_job))
    if paper_job.get("environment") != "paper":
        failures.append("paper qualification does not use the protected paper environment")
    if "secrets." not in json.dumps(paper_job, sort_keys=True):
        failures.append("paper qualification has no explicit protected credential inputs")
    paper_run_text = _run_text(paper_job)
    if "qualify_paper.py candidate" not in paper_run_text:
        failures.append("paper qualification does not bind the downloaded candidate artifact")
    if '"$(uv python find 3.12)" scripts/qualification/qualify_paper.py candidate' not in (
        paper_run_text
    ):
        failures.append("paper artifact binding does not use the uv-managed Python interpreter")
    for provider in ("alpaca", "ib"):
        for phase in ("exercise", "restart"):
            if (
                f"--provider {provider}" not in paper_run_text
                or f"--phase {phase}" not in paper_run_text
            ):
                failures.append(f"paper qualification lacks {provider} {phase} evidence")
    if "qualify_paper.py assemble" not in paper_run_text:
        failures.append("paper qualification does not require a complete redacted evidence bundle")
    if 'qualify_feeds.py" okx' not in paper_run_text:
        failures.append("paper qualification does not run external OKX feed evidence")
    if "scripts.qualification.qualify_provider_soaks" not in paper_run_text:
        failures.append("paper qualification does not run concurrent supported-provider soaks")
    for provider in ("alpaca", "ib"):
        if f"{provider}-soak.json" not in paper_run_text:
            failures.append(f"paper qualification does not retain the {provider} soak")
    if 'qualify_feeds.py" assemble' not in paper_run_text:
        failures.append("paper qualification does not require the installed feed support contract")
    if paper_job.get("timeout-minutes") != "390":
        failures.append("paper qualification timeout cannot contain the six-hour provider soak")
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
    retained_paths = (
        set(str(uploads[0].get("with", {}).get("path", "")).splitlines())
        if len(uploads) == 1
        else set()
    )
    if retained_paths != {"paper-evidence/", "feed-evidence/"}:
        failures.append("paper qualification does not retain its evidence bundle")

    allowed_job_writes = {
        ("release.yml", "publish"): {"id-token"},
        ("release.yml", "github-release"): {"contents"},
        ("release.yml", "recovery-publish"): {"id-token"},
        ("release.yml", "recovery-github-release"): {"contents"},
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
