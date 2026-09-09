"""Behavioral tests for release preflight decisions."""

from __future__ import annotations

import pytest

from scripts.qualification.check_release_preflight import (
    PublicationState,
    preflight_failures,
    version_failure,
)

COMMIT = "a" * 40


@pytest.mark.parametrize("version", ["1.2", "v1.2.3", "1.2.3rc1", "1.2.3.dev1", "1.2.3+local"])
def test_non_stable_release_versions_are_rejected(version: str) -> None:
    assert version_failure(version) is not None


@pytest.mark.parametrize(
    ("publication", "expected"),
    [
        (PublicationState(True, False, False), "Git tag"),
        (PublicationState(False, True, False), "GitHub release"),
        (PublicationState(False, False, True), "PyPI version"),
    ],
)
def test_existing_publication_records_block_a_release(
    publication: PublicationState, expected: str
) -> None:
    failures = preflight_failures(
        version="1.2.3",
        candidate_sha=COMMIT,
        checked_out_sha=COMMIT,
        workflow_sha=COMMIT,
        main_sha=COMMIT,
        publication=publication,
    )

    assert any(expected in failure for failure in failures)


def test_exact_unpublished_main_candidate_passes() -> None:
    assert (
        preflight_failures(
            version="1.2.3",
            candidate_sha=COMMIT,
            checked_out_sha=COMMIT,
            workflow_sha=COMMIT,
            main_sha=COMMIT,
            publication=PublicationState(False, False, False),
        )
        == []
    )


def test_candidate_must_be_the_checked_out_current_main_revision() -> None:
    failures = preflight_failures(
        version="1.2.3",
        candidate_sha=COMMIT,
        checked_out_sha="b" * 40,
        workflow_sha="d" * 40,
        main_sha="c" * 40,
        publication=PublicationState(False, False, False),
    )

    assert "checked-out commit differs from the requested candidate" in failures
    assert "workflow revision differs from the requested candidate" in failures
    assert "candidate commit is not the current origin/main revision" in failures
