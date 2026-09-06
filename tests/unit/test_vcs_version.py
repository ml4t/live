"""Tests for unforced VCS package-version validation."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from scripts.qualification.verify_vcs_version import vcs_version_failure, wheel_version


def _wheel(directory: Path, version: str) -> Path:
    path = directory / f"ml4t_live-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"ml4t_live-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.4\nName: ml4t-live\nVersion: {version}\n",
        )
    return path


@pytest.mark.parametrize(
    "version",
    ("0.1.1", "0.1.1.dev10+gf3135b8", "0.1.2.dev1+gf69079b"),
)
def test_expected_release_transition_passes(version: str) -> None:
    assert vcs_version_failure(version, "0.1.1") is None


@pytest.mark.parametrize(
    "version",
    ("31362844984.dev10", "0.1.3.dev1", "0.2.0.dev1", "0.1.1.post1", "0.1.2"),
)
def test_other_tag_series_and_release_versions_fail(version: str) -> None:
    assert vcs_version_failure(version, "0.1.1") is not None


def test_wheel_metadata_version_is_read(tmp_path: Path) -> None:
    _wheel(tmp_path, "0.1.1.dev10")
    assert wheel_version(tmp_path) == "0.1.1.dev10"


def test_wheel_inventory_must_be_unique(tmp_path: Path) -> None:
    _wheel(tmp_path, "0.1.1")
    _wheel(tmp_path, "0.1.1.dev1")
    with pytest.raises(ValueError, match="exactly one wheel"):
        wheel_version(tmp_path)
