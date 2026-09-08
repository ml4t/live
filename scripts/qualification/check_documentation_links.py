"""Validate Markdown links in the README and documentation tree."""

from __future__ import annotations

import argparse
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
LINK_PATTERN = re.compile(r"!?\[[^]\n]*\]\((?P<target>[^)\s]+)(?:\s+[^)]*)?\)")
REFERENCE_PATTERN = re.compile(r"^\s*\[[^]]+\]:\s*(?P<target>\S+)", re.MULTILINE)
IGNORED_SCHEMES = {"mailto", "tel"}


def documentation_sources(root: Path = REPOSITORY_ROOT) -> tuple[Path, ...]:
    """Return every maintained README and documentation Markdown file."""
    paths = {root / "README.md", root / "examples" / "README.md"}
    paths.update((root / "docs").rglob("*.md"))
    return tuple(sorted(path for path in paths if path.is_file()))


def markdown_targets(path: Path) -> tuple[str, ...]:
    """Extract inline and reference-style Markdown link targets."""
    text = path.read_text(encoding="utf-8")
    targets = [match.group("target") for match in LINK_PATTERN.finditer(text)]
    targets.extend(match.group("target") for match in REFERENCE_PATTERN.finditer(text))
    return tuple(targets)


def resolve_local_target(source: Path, target: str) -> Path | None:
    """Resolve a local Markdown target, allowing MkDocs extensionless links."""
    parsed = urllib.parse.urlsplit(target)
    if not parsed.path or parsed.scheme in IGNORED_SCHEMES or parsed.scheme in {"http", "https"}:
        return None
    if parsed.scheme:
        raise ValueError(f"unsupported link scheme: {target}")
    candidate = (source.parent / urllib.parse.unquote(parsed.path)).resolve()
    choices = (candidate, candidate.with_suffix(".md"), candidate / "index.md")
    return next((choice for choice in choices if choice.exists()), candidate)


def local_link_failures(paths: Iterable[Path]) -> tuple[list[str], set[str], int]:
    """Return missing local targets, external URLs, and total checked links."""
    failures: list[str] = []
    external: set[str] = set()
    checked = 0
    for source in paths:
        for target in markdown_targets(source):
            checked += 1
            parsed = urllib.parse.urlsplit(target)
            if parsed.scheme in {"http", "https"}:
                external.add(target)
                continue
            try:
                resolved = resolve_local_target(source, target)
            except ValueError as error:
                failures.append(f"{source}: {error}")
                continue
            if resolved is not None and not resolved.exists():
                failures.append(f"{source}: missing target {target}")
    return failures, external, checked


def external_link_failure(url: str, *, attempts: int = 3) -> str | None:
    """Return an HTTP failure after bounded retries, or None when the target responds."""
    request = urllib.request.Request(url, headers={"User-Agent": "ml4t-live-link-check/1"})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                if 200 <= response.status < 400:
                    return None
                failure = f"HTTP {response.status}"
        except urllib.error.HTTPError as error:
            if error.code in {401, 403, 429}:
                return None
            failure = f"HTTP {error.code}"
        except (TimeoutError, urllib.error.URLError) as error:
            failure = str(error.reason if isinstance(error, urllib.error.URLError) else error)
        if attempt + 1 < attempts:
            time.sleep(1 + attempt)
    return f"{url}: {failure}"


def check_links(root: Path = REPOSITORY_ROOT, *, external: bool = False) -> list[str]:
    """Return all local and optionally external documentation link failures."""
    failures, external_urls, checked = local_link_failures(documentation_sources(root))
    if external:
        failures.extend(
            failure
            for url in sorted(external_urls)
            if (failure := external_link_failure(url)) is not None
        )
    print(
        f"documentation links checked: {checked} total, "
        f"{len(external_urls)} unique external, external={'yes' if external else 'no'}"
    )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--external", action="store_true")
    args = parser.parse_args()
    failures = check_links(external=args.external)
    for failure in failures:
        print(f"documentation link failure: {failure}")
    if not failures:
        print("documentation link qualification: PASS")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
