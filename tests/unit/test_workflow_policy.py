from __future__ import annotations

from copy import deepcopy

import pytest

from scripts.qualification.check_workflows import (
    WORKFLOW_ROOT,
    action_pin_failures,
    load_workflow,
    paper_runtime_failures,
    paper_soak_failures,
    promotion_failures,
    release_paper_runtime_failures,
    release_recovery_failures,
    validate_workflows,
)


def test_repository_workflows_satisfy_release_policy() -> None:
    assert validate_workflows() == []


def test_every_external_action_is_immutable_and_updateable() -> None:
    assert action_pin_failures(WORKFLOW_ROOT.glob("*.yml")) == []


def test_paper_soak_requires_every_short_provider_check() -> None:
    paper = load_workflow(WORKFLOW_ROOT / "paper.yml")
    paper_job = paper["jobs"]["paper"]

    assert paper_soak_failures(paper_job) == []

    seeded_job = deepcopy(paper_job)
    soak = next(step for step in seeded_job["steps"] if step.get("id") == "provider-soaks")
    soak["if"] = str(soak["if"]).replace("steps.ib-exercise.outcome", "")

    assert any("ib-exercise" in failure for failure in paper_soak_failures(seeded_job))


def test_paper_qualification_uses_a_clean_explicit_runtime() -> None:
    paper = load_workflow(WORKFLOW_ROOT / "paper.yml")
    paper_job = paper["jobs"]["paper"]

    assert paper_runtime_failures(paper_job) == []

    seeded_job = deepcopy(paper_job)
    runtime = next(
        step for step in seeded_job["steps"] if step.get("id") == "qualification-runtime"
    )
    runtime["run"] = str(runtime["run"]).replace('"psutil==7.2.2"', "")

    assert any(
        "pinned runtime dependency" in failure for failure in paper_runtime_failures(seeded_job)
    )


@pytest.mark.parametrize("mutation", ["dependency", "interpreter"])
def test_release_paper_evidence_uses_a_clean_explicit_runtime(mutation: str) -> None:
    release = load_workflow(WORKFLOW_ROOT / "release.yml")
    paper_job = release["jobs"]["paper-evidence"]

    assert release_paper_runtime_failures(paper_job) == []

    seeded_job = deepcopy(paper_job)
    if mutation == "dependency":
        runtime = next(step for step in seeded_job["steps"] if step.get("id") == "paper-runtime")
        runtime["run"] = str(runtime["run"]).replace('"psutil==7.2.2"', "")
        expected = "pinned runtime dependency"
    else:
        paper = next(step for step in seeded_job["steps"] if step.get("id") == "paper")
        paper["run"] = str(paper["run"]).replace(
            '"${RUNNER_TEMP}/paper-evidence-venv/bin/python"', "python"
        )
        expected = "clean validation environment"

    assert any(expected in failure for failure in release_paper_runtime_failures(seeded_job))


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("source", "every mandatory"),
        ("deterministic", "every mandatory"),
        ("dependency", "every mandatory"),
        ("stress", "every mandatory"),
        ("performance", "every mandatory"),
        ("documentation", "every mandatory"),
        ("artifact", "candidate build"),
        ("security", "candidate build"),
        ("publish", "qualification"),
        ("paper-hash", "paper-qualified wheel hash"),
        ("always", "success dependency"),
    ],
)
def test_seeded_mandatory_failure_cannot_reach_publish(mutation: str, expected: str) -> None:
    qualification = load_workflow(WORKFLOW_ROOT / "stable-qualification.yml")
    release = load_workflow(WORKFLOW_ROOT / "release.yml")
    seeded_qualification = deepcopy(qualification)
    seeded_release = deepcopy(release)

    if mutation in {
        "source",
        "deterministic",
        "dependency",
        "stress",
        "performance",
        "documentation",
    }:
        seeded_qualification["jobs"]["build"]["needs"].remove(
            "source-quality" if mutation == "source" else mutation
        )
    elif mutation == "artifact":
        seeded_qualification["jobs"]["artifact-qualification"]["needs"] = []
    elif mutation == "security":
        seeded_qualification["jobs"]["security"]["needs"] = []
    elif mutation == "publish":
        seeded_release["jobs"]["publish"]["needs"] = "github-release"
    elif mutation == "paper-hash":
        del seeded_release["jobs"]["paper-evidence"]["outputs"]
    else:
        seeded_release["jobs"]["publish"]["if"] = "always()"

    assert any(
        expected in failure for failure in promotion_failures(seeded_qualification, seeded_release)
    )


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("missing-paper", "fresh paper evidence"),
        ("wrong-source-run", "exact source-run artifact"),
        ("public-write", "mutate public"),
        ("missing-policy", "check_release_recovery.py"),
        ("missing-identity", "verify_release_identity.py"),
    ],
)
def test_seeded_recovery_failure_is_rejected(mutation: str, expected: str) -> None:
    recovery = load_workflow(WORKFLOW_ROOT / "release-recovery.yml")
    seeded_recovery = deepcopy(recovery)
    verify = seeded_recovery["jobs"]["verify"]

    if mutation == "missing-paper":
        verify["needs"] = []
    elif mutation == "wrong-source-run":
        download = next(
            step
            for step in verify["steps"]
            if str(step.get("uses", "")).startswith("actions/download-artifact@")
        )
        download["with"]["run-id"] = "123"
    elif mutation == "public-write":
        verify["steps"].append({"run": "gh release create v1.2.3"})
    elif mutation == "missing-policy":
        policy = verify["steps"][-1]
        policy["run"] = str(policy["run"]).replace("check_release_recovery.py", "true")
    else:
        identity = verify["steps"][-2]
        identity["run"] = str(identity["run"]).replace("verify_release_identity.py", "true")

    assert any(expected in failure for failure in release_recovery_failures(seeded_recovery))
