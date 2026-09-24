"""Check pinned public book file links against the companion Git tree."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BOOK_URL = re.compile(
    r"https://github\.com/stefan-jansen/machine-learning-for-trading/blob/"
    r"(?P<revision>[0-9a-f]{40})/(?P<path>[^\s)]+?\.(?:ipynb|py))"
)


def check_book_links(companion_repo: Path) -> tuple[int, list[str]]:
    """Return the number of checked file links and any revision or path failures."""
    links: list[tuple[Path, str, str]] = []
    for source in sorted((REPOSITORY_ROOT / "docs").rglob("*.md")):
        for match in BOOK_URL.finditer(source.read_text(encoding="utf-8")):
            links.append((source, match.group("revision"), match.group("path")))

    failures: list[str] = []
    if not links:
        return 0, ["no pinned book file links found"]
    revisions = {revision for _, revision, _ in links}
    if len(revisions) != 1:
        failures.append(f"book file links use multiple revisions: {sorted(revisions)}")
        return len(links), failures

    revision = revisions.pop()
    result = subprocess.run(
        ["git", "-C", str(companion_repo), "ls-tree", "-r", "--name-only", revision],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        return len(links), [
            f"companion revision {revision} is unavailable: {result.stderr.strip()}"
        ]
    tree = set(result.stdout.splitlines())
    for source, _, path in links:
        if path not in tree:
            failures.append(f"{source.relative_to(REPOSITORY_ROOT)}: missing book file {path}")

    guide = REPOSITORY_ROOT / "docs" / "book-guide" / "index.md"
    guide_count = len(BOOK_URL.findall(guide.read_text(encoding="utf-8")))
    if guide_count < 14:
        failures.append(f"Book Guide has {guide_count} direct file links; expected at least 14")
    return len(links), failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--companion-repo", type=Path, required=True)
    args = parser.parse_args()
    count, failures = check_book_links(args.companion_repo)
    print(f"checked {count} book file links")
    for failure in failures:
        print(f"book link failure: {failure}")
    if not failures:
        print("book links: PASS")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
