"""Verify PyPI and GitHub records against the immutable release manifest."""

from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from packaging.specifiers import SpecifierSet

try:
    from scripts.qualification.qualify_artifacts import (
        EXPECTED_AUTHOR,
        EXPECTED_CLASSIFIERS,
        EXPECTED_DESCRIPTION,
        EXPECTED_KEYWORDS,
        EXPECTED_MAINTAINER,
        EXPECTED_URLS,
    )
    from scripts.qualification.verify_release_identity import HASH_PATTERN, load_json
except ModuleNotFoundError:
    from qualify_artifacts import (
        EXPECTED_AUTHOR,
        EXPECTED_CLASSIFIERS,
        EXPECTED_DESCRIPTION,
        EXPECTED_KEYWORDS,
        EXPECTED_MAINTAINER,
        EXPECTED_URLS,
    )
    from verify_release_identity import HASH_PATTERN, load_json


def _artifact_digests(manifest: dict[str, Any]) -> dict[str, str]:
    records = manifest.get("artifacts", {})
    if not isinstance(records, dict):
        return {}
    return {
        str(record.get("filename", "")): str(record.get("sha256", ""))
        for record in records.values()
        if isinstance(record, dict)
    }


def published_release_failures(
    manifest: dict[str, Any],
    pypi: dict[str, Any],
    release: dict[str, Any],
) -> list[str]:
    """Return mismatches across the manifest, PyPI JSON, and GitHub release."""
    failures: list[str] = []
    version = str(manifest.get("version", ""))
    commit = str(manifest.get("commit", ""))
    artifacts = _artifact_digests(manifest)
    if (
        manifest.get("schema_version") != 1
        or manifest.get("distribution") != "ml4t-live"
        or not version
        or len(commit) != 40
        or not artifacts
        or any(not HASH_PATTERN.fullmatch(digest) for digest in artifacts.values())
    ):
        return ["release manifest is incomplete"]

    info = pypi.get("info", {})
    keywords = {
        keyword.strip() for keyword in str(info.get("keywords", "")).split(",") if keyword.strip()
    }
    pypi_files = {
        str(record.get("filename", "")): str(record.get("digests", {}).get("sha256", ""))
        for record in pypi.get("urls", [])
        if isinstance(record, dict)
    }
    if (
        info.get("name") != "ml4t-live"
        or info.get("version") != version
        or info.get("summary") != EXPECTED_DESCRIPTION
        or info.get("author_email") != EXPECTED_AUTHOR
        or info.get("maintainer_email") != EXPECTED_MAINTAINER
        or info.get("license_expression") != "MIT"
        or SpecifierSet(str(info.get("requires_python", ""))) != SpecifierSet(">=3.12")
        or info.get("project_urls") != EXPECTED_URLS
        or keywords != EXPECTED_KEYWORDS
        or not EXPECTED_CLASSIFIERS <= set(info.get("classifiers", []))
    ):
        failures.append("PyPI metadata differs from the qualified source")
    if pypi_files != artifacts:
        failures.append("PyPI artifact names or digests differ from the release manifest")

    release_assets = {
        str(asset.get("name", "")): str(asset.get("digest", "")).removeprefix("sha256:")
        for asset in release.get("assets", [])
        if isinstance(asset, dict) and str(asset.get("name", "")) in artifacts
    }
    if release.get("tag_name") != f"v{version}" or release.get("target_commitish") != commit:
        failures.append("GitHub release tag or target differs from the release manifest")
    if release_assets != artifacts:
        failures.append("GitHub release artifact names or digests differ from the release manifest")
    return failures


def _read_json(url: str, *, token: str | None = None) -> dict[str, Any]:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "ml4t-live-release"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError(f"{url} did not return a JSON object")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--repository", default="ml4t/live")
    args = parser.parse_args()
    manifest = load_json(args.manifest)
    version = urllib.parse.quote(str(manifest.get("version", "")), safe="")
    tag = urllib.parse.quote(f"v{manifest.get('version', '')}", safe="")
    failures = published_release_failures(
        manifest,
        _read_json(f"https://pypi.org/pypi/ml4t-live/{version}/json"),
        _read_json(
            f"https://api.github.com/repos/{args.repository}/releases/tags/{tag}",
            token=os.environ.get("GITHUB_TOKEN"),
        ),
    )
    print(f"published release identity: {'PASS' if not failures else 'FAIL'}")
    for failure in failures:
        print(f"- {failure}")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
