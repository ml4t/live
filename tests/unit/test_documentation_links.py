from __future__ import annotations

from pathlib import Path

from scripts.qualification.check_documentation_links import (
    check_links,
    documentation_sources,
    markdown_targets,
)


def test_documentation_sources_include_readmes_and_docs(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("root")
    (tmp_path / "examples").mkdir()
    (tmp_path / "examples" / "README.md").write_text("examples")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "index.md").write_text("docs")

    assert {path.relative_to(tmp_path) for path in documentation_sources(tmp_path)} == {
        Path("README.md"),
        Path("examples/README.md"),
        Path("docs/index.md"),
    }


def test_markdown_targets_extract_inline_images_and_references(tmp_path: Path) -> None:
    page = tmp_path / "page.md"
    page.write_text(
        "[page](other.md#section)\n![image](image.png)\n[external][docs]\n"
        "[docs]: https://example.com/docs\n"
    )

    assert markdown_targets(page) == (
        "other.md#section",
        "image.png",
        "https://example.com/docs",
    )


def test_check_links_detects_missing_local_target(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("[missing](docs/missing.md)\n")

    failures = check_links(tmp_path)

    assert len(failures) == 1
    assert "missing target docs/missing.md" in failures[0]


def test_check_links_accepts_existing_extensionless_page(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("[guide](docs/guide/)\n")
    guide = tmp_path / "docs" / "guide"
    guide.mkdir(parents=True)
    (guide / "index.md").write_text("# Guide\n")

    assert check_links(tmp_path) == []
