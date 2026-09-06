"""Verify that an unforced VCS build derives from the intended package tag series."""

from __future__ import annotations

import argparse
import email
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts-dir", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args()
    try:
        version = wheel_version(args.artifacts_dir)
        failure = vcs_version_failure(version, args.expected_version)
    except (ValueError, zipfile.BadZipFile) as error:
        version = "unavailable"
        failure = str(error)
    print(f"VCS package version: {'PASS' if failure is None else 'FAIL'} ({version})")
    if failure:
        print(f"- {failure}")
    return int(failure is not None)


if __name__ == "__main__":
    raise SystemExit(main())
