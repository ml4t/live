"""Tests for the complete stable-surface inventory."""

from __future__ import annotations

import runpy
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).parents[2]
CAPTURE = runpy.run_path(str(ROOT / "scripts" / "qualification" / "capture_stable_surface.py"))


def test_source_and_runtime_public_definitions_agree() -> None:
    capture_surface = cast(Callable[[Path], dict[str, Any]], CAPTURE["capture_surface"])

    surface = capture_surface(ROOT / "src")

    assert surface["source_runtime_mismatches"] == []
    assert surface["root_exports"]
    assert all(item["classification"] for item in surface["symbols"])


def test_surface_classifies_stable_experimental_and_internal_symbols() -> None:
    capture_surface = cast(Callable[[Path], dict[str, Any]], CAPTURE["capture_surface"])
    surface = capture_surface(ROOT / "src")
    symbols = {(item["module"], item["name"]): item for item in surface["symbols"]}

    assert symbols[("ml4t.live", "LiveEngine")]["classification"] == "stable"
    assert symbols[("ml4t.live", "DataBentoFeed")]["classification"] == "experimental"
    assert symbols[("ml4t.live.cli.main", "NullBroker")]["classification"] == "internal"
    assert symbols[("ml4t.live.safety", "LiveRiskConfig")]["dataclass_fields"]
    assert symbols[("ml4t.live.safety", "RiskLimitError")]["kind"] == "exception"
    assert "typing." not in symbols[("ml4t.live", "LiveEngine")]["signature"]


def test_surface_includes_cli_entry_point_and_persisted_schema() -> None:
    capture_surface = cast(Callable[[Path], dict[str, Any]], CAPTURE["capture_surface"])

    surface = capture_surface(ROOT / "src")

    assert surface["entry_points"] == {"ml4t-live": "ml4t.live.cli.main:app"}
    assert set(surface["cli"]) == {"preflight", "shadow", "status"}
    assert surface["persisted_schemas"]["risk_state"]["version"] == 1
    assert "portable_strategy_state" in surface["persisted_schemas"]["risk_state"]["fields"]
