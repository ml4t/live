"""Tests for immutable published release verification."""

from __future__ import annotations

from copy import deepcopy

import pytest

from scripts.qualification.qualify_artifacts import (
    EXPECTED_AUTHOR,
    EXPECTED_CLASSIFIERS,
    EXPECTED_DESCRIPTION,
    EXPECTED_KEYWORDS,
    EXPECTED_MAINTAINER,
    EXPECTED_URLS,
)
from scripts.qualification.verify_published_release import published_release_failures

COMMIT = "a" * 40
ARTIFACTS = {
    "wheel": {"filename": "ml4t_live-1.2.3-py3-none-any.whl", "sha256": "b" * 64},
    "sdist": {"filename": "ml4t_live-1.2.3.tar.gz", "sha256": "c" * 64},
}


def published_fixture() -> tuple[dict, dict, dict]:
    manifest = {
        "schema_version": 1,
        "distribution": "ml4t-live",
        "version": "1.2.3",
        "commit": COMMIT,
        "artifacts": deepcopy(ARTIFACTS),
    }
    pypi = {
        "info": {
            "name": "ml4t-live",
            "version": "1.2.3",
            "summary": EXPECTED_DESCRIPTION,
            "author_email": EXPECTED_AUTHOR,
            "maintainer_email": EXPECTED_MAINTAINER,
            "license_expression": "MIT",
            "requires_python": ">=3.12",
            "project_urls": EXPECTED_URLS,
            "keywords": ", ".join(sorted(EXPECTED_KEYWORDS)),
            "classifiers": sorted(EXPECTED_CLASSIFIERS),
        },
        "urls": [
            {"filename": record["filename"], "digests": {"sha256": record["sha256"]}}
            for record in ARTIFACTS.values()
        ],
    }
    release = {
        "tag_name": "v1.2.3",
        "target_commitish": COMMIT,
        "assets": [
            {"name": record["filename"], "digest": f"sha256:{record['sha256']}"}
            for record in ARTIFACTS.values()
        ],
    }
    return manifest, pypi, release


def test_matching_pypi_and_github_release_pass() -> None:
    assert published_release_failures(*published_fixture()) == []


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("manifest", "manifest"),
        ("metadata", "PyPI metadata"),
        ("pypi-file", "PyPI artifact"),
        ("tag", "GitHub release tag"),
        ("release-file", "GitHub release artifact"),
    ],
)
def test_published_identity_drift_is_rejected(target: str, expected: str) -> None:
    manifest, pypi, release = published_fixture()
    if target == "manifest":
        manifest["commit"] = "short"
    elif target == "metadata":
        pypi["info"]["summary"] = "different"
    elif target == "pypi-file":
        pypi["urls"][0]["digests"]["sha256"] = "d" * 64
    elif target == "tag":
        release["tag_name"] = "v1.2.4"
    else:
        release["assets"][0]["digest"] = "sha256:" + "d" * 64

    assert any(
        expected in failure for failure in published_release_failures(manifest, pypi, release)
    )
