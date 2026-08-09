"""Tests for immutable release-recovery policy."""

from __future__ import annotations

import tomllib
from copy import deepcopy

from scripts.qualification.check_release_recovery import POLICY_PATH, recovery_failures


def load_policy() -> dict:
    """Load the repository recovery policy."""
    return tomllib.loads(POLICY_PATH.read_text())


def test_repository_release_recovery_policy_is_complete() -> None:
    assert recovery_failures(load_policy()) == []


def test_recovery_rejects_replacement_of_immutable_artifacts() -> None:
    policy = deepcopy(load_policy())
    policy["scenarios"]["partial_pypi_publish"]["replacement_allowed"] = True

    assert recovery_failures(policy) == ["partial_pypi_publish recovery permits replacement"]


def test_recovery_requires_new_version_after_identity_conflict() -> None:
    policy = deepcopy(load_policy())
    policy["scenarios"]["tag_conflict"]["requires_new_version_on_conflict"] = False

    assert recovery_failures(policy) == [
        "tag_conflict recovery does not require a new version after conflict"
    ]
