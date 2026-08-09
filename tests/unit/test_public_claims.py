"""Qualification tests for public beta claims and maintained examples."""

from pathlib import Path

from scripts.qualification.check_public_claims import (
    DETERMINISTIC_EXAMPLES,
    EXTERNAL_EXAMPLES,
    check_chapter_claims,
    check_public_claims,
)

ROOT = Path(__file__).parents[2]


def test_public_claims_match_the_qualified_contract() -> None:
    assert check_public_claims(ROOT) == []


def test_every_maintained_example_has_one_qualification_class() -> None:
    examples = {path.name for path in (ROOT / "examples").glob("*.py")}
    assert not DETERMINISTIC_EXAMPLES & EXTERNAL_EXAMPLES
    assert examples == DETERMINISTIC_EXAMPLES | EXTERNAL_EXAMPLES


def test_chapter_scan_requires_both_engines_lifecycle_and_intents(tmp_path: Path) -> None:
    book_root = tmp_path / "book"
    code_root = tmp_path / "code"
    section_root = book_root / "25_live_trading/chapter/sections"
    section_root.mkdir(parents=True)
    for name in (
        "section_00_intro.md",
        "section_01_the_unified_researchtoproduction_framewo.md",
        "section_06_ensuring_technical_parity_through_pipeli.md",
        "section_08_summary.md",
    ):
        (section_root / name).write_text("lifecycle version 1 canonical intent outcome parity\n")
    code_chapter = code_root / "25_live_trading"
    code_chapter.mkdir(parents=True)
    (code_chapter / "01_unified_framework_demo.py").write_text(
        "Engine(feed)\nLiveEngine(feed)\ncallback_trace\nCanonicalTargetIntent\n"
    )
    (code_chapter / "README.md").write_text("qualified boundaries\n")

    assert check_chapter_claims(book_root, code_root) == []
    (code_chapter / "01_unified_framework_demo.py").write_text("Engine(feed)\nLiveEngine(feed)\n")
    failures = check_chapter_claims(book_root, code_root)
    assert "chapter parity demo does not exercise: callback_trace" in failures
    assert "chapter parity demo does not exercise: CanonicalTargetIntent" in failures
