"""Check text file endings without modifying candidate files."""

from __future__ import annotations

import sys
from pathlib import Path


def violations(path: Path) -> list[str]:
    data = path.read_bytes()
    if not data or b"\0" in data:
        return []

    failures = []
    if b"\r" in data:
        failures.append("contains CR or CRLF line endings")
    if not data.endswith(b"\n"):
        failures.append("does not end with a newline")
    for line_number, line in enumerate(data.splitlines(), start=1):
        if line.endswith((b" ", b"\t")):
            failures.append(f"line {line_number} has trailing whitespace")
    return failures


def main(paths: list[str] | None = None) -> int:
    failures = []
    for raw_path in paths if paths is not None else sys.argv[1:]:
        path = Path(raw_path)
        failures.extend(f"{path}: {failure}" for failure in violations(path))
    if failures:
        print("\n".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
