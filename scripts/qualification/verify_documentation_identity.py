"""Verify local or deployed ML4T documentation release identity."""

from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

REQUIRED_META_NAMES = ("ml4t-library", "ml4t-version", "ml4t-commit")
REPRESENTATIVE_PATHS = ("", "getting-started/quickstart/", "api/")


class MetadataParser(HTMLParser):
    """Collect named HTML metadata values."""

    def __init__(self) -> None:
        super().__init__()
        self.metadata: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta":
            return
        values = dict(attrs)
        name = values.get("name")
        content = values.get("content")
        if name in REQUIRED_META_NAMES and content is not None:
            self.metadata[name] = content


def html_identity_failures(html: str, expected: dict[str, str], label: str) -> list[str]:
    """Return metadata mismatches for one rendered page."""
    parser = MetadataParser()
    parser.feed(html)
    return [
        f"{label}: {name} is {parser.metadata.get(name)!r}, expected {value!r}"
        for name, value in expected.items()
        if parser.metadata.get(name) != value
    ]


def expected_identity(library: str, version: str, commit: str) -> dict[str, str]:
    """Return the canonical HTML metadata mapping."""
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise ValueError("commit must be a full lowercase hexadecimal SHA")
    return {
        "ml4t-library": library,
        "ml4t-version": version,
        "ml4t-commit": commit,
    }


def site_identity_failures(site_dir: Path, *, library: str, version: str, commit: str) -> list[str]:
    """Validate every rendered page and the release identity manifest."""
    expected = expected_identity(library, version, commit)
    html_paths = sorted(site_dir.rglob("*.html"))
    failures = [] if html_paths else [f"{site_dir}: no rendered HTML pages"]
    for path in html_paths:
        failures.extend(
            html_identity_failures(path.read_text(encoding="utf-8"), expected, str(path))
        )
    manifest_path = site_dir / "release.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        failures.append(f"{manifest_path}: unreadable release identity: {error}")
    else:
        expected_manifest = {"library": library, "version": version, "commit": commit}
        if manifest != expected_manifest:
            failures.append(f"{manifest_path}: identity mismatch")
    return failures


def deployed_identity_failures(
    base_url: str,
    *,
    library: str,
    version: str,
    commit: str,
    attempts: int = 12,
    wait_seconds: float = 10.0,
) -> list[str]:
    """Poll representative deployed pages until every identity matches."""
    expected = expected_identity(library, version, commit)
    last_failures: list[str] = []
    for attempt in range(attempts):
        failures = []
        for relative in REPRESENTATIVE_PATHS:
            url = urllib.parse.urljoin(base_url, relative)
            try:
                with urllib.request.urlopen(url, timeout=20) as response:
                    html = response.read().decode("utf-8")
            except Exception as error:
                failures.append(f"{url}: {error}")
                continue
            failures.extend(html_identity_failures(html, expected, url))
        if not failures:
            return []
        last_failures = failures
        if attempt + 1 < attempts:
            time.sleep(wait_seconds)
    return last_failures


def main() -> int:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--site-dir", type=Path)
    source.add_argument("--url")
    parser.add_argument("--library", default="live")
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    kwargs = {"library": args.library, "version": args.version, "commit": args.commit}
    failures = (
        site_identity_failures(args.site_dir, **kwargs)
        if args.site_dir
        else deployed_identity_failures(args.url, **kwargs)
    )
    for failure in failures:
        print(f"documentation identity failure: {failure}")
    if not failures:
        print("documentation identity qualification: PASS")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
