"""Qualification tests for maintained public claims and examples."""

from pathlib import Path

from scripts.qualification.check_public_claims import (
    DETERMINISTIC_EXAMPLES,
    EXTERNAL_EXAMPLES,
    check_chapter_claims,
    check_public_claims,
    public_surface_paths,
    unsupported_claims,
)

ROOT = Path(__file__).parents[2]


def test_public_claims_match_the_qualified_contract() -> None:
    assert check_public_claims(ROOT) == []


def test_every_maintained_example_has_one_qualification_class() -> None:
    examples = {path.name for path in (ROOT / "examples").glob("*.py")}
    assert not DETERMINISTIC_EXAMPLES & EXTERNAL_EXAMPLES
    assert examples == DETERMINISTIC_EXAMPLES | EXTERNAL_EXAMPLES


def test_external_examples_document_safe_operation() -> None:
    for name in EXTERNAL_EXAMPLES:
        text = (ROOT / "examples" / name).read_text()
        for heading in ("Prerequisites:", "Expected Output:", "Expected Failure:", "Cleanup:"):
            assert heading in text
        if name != "okx_funding_paper.py":
            assert "paper" in text.casefold()


def test_public_surface_discovery_covers_docs_examples_and_package_source(tmp_path: Path) -> None:
    expected = (
        tmp_path / "docs/new-page.md",
        tmp_path / "examples/new-example.py",
        tmp_path / "src/ml4t/live/new_module.py",
    )
    for path in expected:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("qualified claim\n")

    discovered = public_surface_paths(tmp_path)
    assert all(path in discovered for path in expected)


def test_unsupported_claims_identify_the_source_file(tmp_path: Path) -> None:
    path = tmp_path / "docs/new-page.md"
    path.parent.mkdir(parents=True)
    path.write_text("The code works unchanged in production.\n")

    failures = unsupported_claims((path,))

    assert failures == [f"unsupported absolute claim in {path}: works unchanged in production"]


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
