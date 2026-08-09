from __future__ import annotations

from copy import deepcopy

import pytest

from scripts.qualification.check_workflows import (
    WORKFLOW_ROOT,
    action_pin_failures,
    load_workflow,
    promotion_failures,
    validate_workflows,
)


def test_repository_workflows_satisfy_release_policy() -> None:
    assert validate_workflows() == []


def test_every_external_action_is_immutable_and_updateable() -> None:
    assert action_pin_failures(WORKFLOW_ROOT.glob("*.yml")) == []


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("source", "every mandatory"),
        ("deterministic", "every mandatory"),
        ("dependency", "every mandatory"),
        ("stress", "every mandatory"),
        ("documentation", "every mandatory"),
        ("artifact", "candidate build"),
        ("publish", "complete reusable"),
        ("always", "success dependency"),
    ],
)
def test_seeded_mandatory_failure_cannot_reach_publish(mutation: str, expected: str) -> None:
    qualification = load_workflow(WORKFLOW_ROOT / "qualification.yml")
    release = load_workflow(WORKFLOW_ROOT / "release.yml")
    seeded_qualification = deepcopy(qualification)
    seeded_release = deepcopy(release)

    if mutation in {"source", "deterministic", "dependency", "stress", "documentation"}:
        seeded_qualification["jobs"]["build"]["needs"].remove(
            "source-quality" if mutation == "source" else mutation
        )
    elif mutation == "artifact":
        seeded_qualification["jobs"]["artifact-qualification"]["needs"] = []
    elif mutation == "publish":
        seeded_release["jobs"]["publish"]["needs"] = "github-release"
    else:
        seeded_release["jobs"]["publish"]["if"] = "always()"

    assert any(
        expected in failure for failure in promotion_failures(seeded_qualification, seeded_release)
    )
