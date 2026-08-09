"""Fault-sensitive tests for architecture qualification."""

from __future__ import annotations

import runpy
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).parents[2]
CHECKER = runpy.run_path(str(ROOT / "scripts" / "qualification" / "qualify_architecture.py"))


def test_source_import_graph_is_acyclic() -> None:
    import_graph = cast(Callable[[Path], dict[str, set[str]]], CHECKER["import_graph"])
    import_cycles = cast(Callable[[dict[str, set[str]]], list[list[str]]], CHECKER["import_cycles"])

    assert import_cycles(import_graph(ROOT / "src" / "ml4t" / "live")) == []


def test_import_cycle_is_detected() -> None:
    import_cycles = cast(Callable[[dict[str, set[str]]], list[list[str]]], CHECKER["import_cycles"])

    assert import_cycles({"a": {"b"}, "b": {"c"}, "c": {"a"}}) == [["a", "b", "c"]]


def test_source_has_no_unowned_static_suppression() -> None:
    inventory = cast(Callable[[Path], list[dict[str, Any]]], CHECKER["suppression_inventory"])

    assert inventory(ROOT / "src" / "ml4t" / "live") == []


def test_static_suppression_is_detected(tmp_path: Path) -> None:
    inventory = cast(Callable[[Path], list[dict[str, Any]]], CHECKER["suppression_inventory"])
    source = tmp_path / "unsafe.py"
    source.write_text("value = unknown  # type: ignore[name-defined]\n")

    assert inventory(tmp_path) == [
        {
            "path": str(source),
            "line": 1,
            "marker": "type: ignore",
        }
    ]


def test_public_interfaces_have_complete_annotations() -> None:
    annotation_gaps = cast(
        Callable[[dict[str, object] | None], list[str]], CHECKER["public_annotation_gaps"]
    )

    assert annotation_gaps(None) == []


def test_public_annotation_gap_is_detected() -> None:
    annotation_gaps = cast(
        Callable[[dict[str, object] | None], list[str]], CHECKER["public_annotation_gaps"]
    )

    def incomplete(value):
        return value

    assert annotation_gaps({"incomplete": incomplete}) == [
        "incomplete:value",
        "incomplete:return",
    ]
