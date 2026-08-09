"""Validate maintained public claims, documentation coverage, and examples."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

REQUIRED_PUBLIC_FILES = (
    "README.md",
    "api.yaml",
    "DESIGN.md",
    "examples/README.md",
    "docs/index.md",
    "docs/getting-started/installation.md",
    "docs/user-guide/backtest-to-live.md",
    "docs/user-guide/brokers.md",
    "docs/user-guide/feeds.md",
    "docs/user-guide/risk.md",
    "docs/user-guide/operator-guide.md",
    "docs/user-guide/migration.md",
    "docs/qualification.md",
    "docs/claim-evidence.md",
)

PROHIBITED_CLAIMS = (
    "zero-code migration",
    "zero code changes",
    "zero changes",
    "works unchanged in production",
    "code runs identically in backtest and live",
    "same Strategy class works in both environments",
    "identical code in both backtest and live modes",
    "eliminates technical divergence by construction",
    "reproduced exactly in production",
    "production code waiting for a live data source",
    "same strategy class works in backtest and live",
    "identical outputs in backtest and live modes",
    "same code for backtest and live",
)

REQUIRED_TEXT = {
    "README.md": (
        "lifecycle version 1",
        "Linux",
        "Python 3.12, 3.13, and 3.14",
        "DataBento",
        "experimental",
    ),
    "api.yaml": (
        "lifecycle version 1",
        "execution_mode",
        "max_position_shares",
        "experimental",
        "dedicated worker thread",
    ),
    "DESIGN.md": (
        "Historical design record",
        "does not define current behavior",
    ),
    "docs/user-guide/backtest-to-live.md": (
        "CanonicalTargetIntent",
        "execution policy",
        "outcome parity",
        "on_prepare",
    ),
    "docs/user-guide/migration.md": (
        "on_before_risk",
        "on_historical_data",
        "HistoricalStrategyCompatibilityError",
        "causal initialization",
        "information_cutoff",
        "opening cutoff",
        "reject_ambiguous",
        "position-rule",
    ),
    "docs/user-guide/brokers.md": (
        "CanonicalOrderRequest",
        "reducing-risk",
        "capabilities",
        "reconciliation",
    ),
    "docs/user-guide/operator-guide.md": (
        "startup rollback",
        "recovery gap",
        "overload",
        "audit failure",
    ),
    "docs/qualification.md": (
        "exact candidate commit",
        "does not create a tag",
        "does not publish",
        "does not place a live-money order",
    ),
    "docs/claim-evidence.md": (
        "Portability",
        "Safety",
        "Feeds",
        "Brokers",
        "Performance",
        "Platform",
        "Maturity",
    ),
}

DETERMINISTIC_EXAMPLES = frozenset(
    {
        "risk_guard_demo.py",
        "shadow_mode_demo.py",
        "startup_reconciliation_demo.py",
    }
)
EXTERNAL_EXAMPLES = frozenset(
    {
        "alpaca_paper_equity.py",
        "ib_paper_equity.py",
        "live_ib_example.py",
        "okx_funding_paper.py",
    }
)


def public_surface_paths(root: Path) -> tuple[Path, ...]:
    """Return every maintained public narrative surface in one source tree."""
    paths = {root / relative for relative in ("README.md", "api.yaml", "DESIGN.md")}
    paths.update((root / "docs").rglob("*.md"))
    paths.update((root / "examples").glob("*.md"))
    paths.update((root / "examples").glob("*.py"))
    paths.update((root / "src").rglob("*.py"))
    return tuple(sorted(paths))


def unsupported_claims(paths: tuple[Path, ...], *, prefix: str = "") -> list[str]:
    """Return unsupported absolute claims with their source paths."""
    failures: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        text = path.read_text().casefold()
        for claim in PROHIBITED_CLAIMS:
            if claim.casefold() in text:
                failures.append(f"{prefix}unsupported absolute claim in {path}: {claim}")
    return failures


def check_public_claims(root: Path = REPOSITORY_ROOT) -> list[str]:
    """Return claim and example-policy failures for one source tree."""
    failures: list[str] = []
    texts: dict[str, str] = {}
    for relative in REQUIRED_PUBLIC_FILES:
        path = root / relative
        if not path.is_file():
            failures.append(f"missing public document: {relative}")
            continue
        texts[relative] = path.read_text()

    failures.extend(unsupported_claims(public_surface_paths(root)))

    for relative, required in REQUIRED_TEXT.items():
        text = texts.get(relative, "")
        for phrase in required:
            if phrase.casefold() not in text.casefold():
                failures.append(f"{relative} does not state: {phrase}")

    examples = {path.name for path in (root / "examples").glob("*.py")}
    classified = DETERMINISTIC_EXAMPLES | EXTERNAL_EXAMPLES
    if examples != classified:
        failures.append(
            "example classification mismatch: "
            f"missing={sorted(examples - classified)}; stale={sorted(classified - examples)}"
        )
    for name in sorted(EXTERNAL_EXAMPLES):
        path = root / "examples" / name
        text = path.read_text() if path.is_file() else ""
        for heading in ("Prerequisites:", "Expected Output:", "Expected Failure:", "Cleanup:"):
            if heading not in text:
                failures.append(f"external example {name} does not state: {heading}")
        if name != "okx_funding_paper.py" and "paper" not in text.casefold():
            failures.append(f"external broker example {name} does not identify a paper account")
    return failures


def check_chapter_claims(book_root: Path, code_root: Path) -> list[str]:
    """Return failures in the maintained chapter 25 portability material."""
    failures: list[str] = []
    section_root = book_root / "25_live_trading/chapter/sections"
    code_chapter = code_root / "25_live_trading"
    book_paths = tuple(sorted(section_root.glob("*.md")))
    code_paths = tuple(sorted((*code_chapter.glob("*.py"), *code_chapter.glob("*.md"))))
    required_paths = (
        section_root / "section_00_intro.md",
        section_root / "section_01_the_unified_researchtoproduction_framewo.md",
        section_root / "section_06_ensuring_technical_parity_through_pipeli.md",
        section_root / "section_08_summary.md",
        code_chapter / "01_unified_framework_demo.py",
        code_chapter / "README.md",
    )
    for path in required_paths:
        if not path.is_file():
            failures.append(f"missing chapter file: {path}")

    book_text = "\n".join(path.read_text() for path in book_paths if path.is_file())
    code_text = "\n".join(path.read_text() for path in code_paths if path.is_file())
    combined = f"{book_text}\n{code_text}".casefold()
    failures.extend(unsupported_claims((*book_paths, *code_paths), prefix="chapter "))
    for phrase in ("lifecycle version 1", "canonical intent", "outcome parity"):
        if phrase.casefold() not in combined:
            failures.append(f"chapter does not state: {phrase}")
    demo_path = code_chapter / "01_unified_framework_demo.py"
    demo = demo_path.read_text() if demo_path.is_file() else ""
    for phrase in ("Engine(", "LiveEngine(", "callback_trace", "CanonicalTargetIntent"):
        if phrase not in demo:
            failures.append(f"chapter parity demo does not exercise: {phrase}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--book-root", type=Path)
    parser.add_argument("--code-root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    failures = check_public_claims()
    if bool(args.book_root) != bool(args.code_root):
        failures.append("--book-root and --code-root must be provided together")
    if args.book_root and args.code_root:
        failures.extend(check_chapter_claims(args.book_root, args.code_root))

    report = {
        "schema_version": 1,
        "public_files": [
            str(path.relative_to(REPOSITORY_ROOT)) for path in public_surface_paths(REPOSITORY_ROOT)
        ],
        "deterministic_examples": sorted(DETERMINISTIC_EXAMPLES),
        "external_examples": sorted(EXTERNAL_EXAMPLES),
        "chapter_checked": bool(args.book_root),
        "failures": failures,
        "passed": not failures,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if failures:
        for failure in failures:
            print(f"public claim failure: {failure}")
    else:
        print("public claim qualification: PASS")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
