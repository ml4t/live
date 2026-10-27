"""Verify that an unforced VCS build derives from the intended package tag series."""

from __future__ import annotations

import argparse
import email
import json
import os
import re
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from packaging.version import Version


def wheel_version(artifacts_directory: Path) -> str:
    """Read the version from exactly one wheel."""
    wheels = sorted(artifacts_directory.glob("*.whl"))
    if len(wheels) != 1:
        raise ValueError("VCS version check requires exactly one wheel")
    with zipfile.ZipFile(wheels[0]) as archive:
        metadata_files = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_files) != 1:
            raise ValueError("VCS version wheel has no unique METADATA file")
        version = email.message_from_bytes(archive.read(metadata_files[0])).get("Version")
    if not version:
        raise ValueError("VCS version wheel has no version")
    return version


def vcs_version_failure(version_text: str, expected_version: str) -> str | None:
    """Return why an unforced build is outside its intended release series."""
    version = Version(version_text)
    expected = Version(expected_version)
    next_patch = Version(f"{expected.major}.{expected.minor}.{expected.micro + 1}")
    if version == expected:
        return None
    if not version.is_devrelease:
        return f"VCS version {version} is neither {expected} nor a development build"
    if Version(version.base_version) not in {expected, next_patch}:
        return (
            f"VCS version {version} is outside release transition "
            f"{expected.base_version} to {next_patch.base_version}"
        )
    return None


def published_release_version(repository: str) -> str:
    """Use the published stable release as the independent unforced-build baseline."""
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None:
        raise ValueError("invalid GitHub repository")
    headers = {"Accept": "application/vnd.github+json"}
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/releases/latest", headers=headers
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        release = json.load(response)
    tag = release.get("tag_name")
    if (
        not isinstance(tag, str)
        or not tag.startswith("v")
        or release.get("draft") is not False
        or release.get("prerelease") is not False
    ):
        raise ValueError("GitHub did not return a published stable release")
    version = Version(tag[1:])
    if version.is_prerelease or version.is_devrelease or version.is_postrelease or version.local:
        raise ValueError("published release tag is not a stable package version")
    return str(version)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts-dir", type=Path, required=True)
    baseline = parser.add_mutually_exclusive_group(required=True)
    baseline.add_argument("--expected-version")
    baseline.add_argument("--repository")
    args = parser.parse_args()
    try:
        version = wheel_version(args.artifacts_dir)
        expected = (
            published_release_version(args.repository) if args.repository else args.expected_version
        )
        failure = vcs_version_failure(version, expected)
    except (ValueError, OSError, zipfile.BadZipFile) as error:
        version = "unavailable"
        failure = str(error)
    print(f"VCS package version: {'PASS' if failure is None else 'FAIL'} ({version})")
    if failure:
        print(f"- {failure}")
    return int(failure is not None)


if __name__ == "__main__":
    raise SystemExit(main())
