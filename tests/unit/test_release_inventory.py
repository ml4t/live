"""Tests for exact-artifact dependency and SBOM generation."""

from __future__ import annotations

import zipfile
from pathlib import Path

from scripts.qualification.build_release_inventory import build_inventories


def write_artifacts(root: Path) -> None:
    """Create the minimal artifact pair needed by the inventory reader."""
    with zipfile.ZipFile(root / "ml4t_live-1.0.0-py3-none-any.whl", "w") as archive:
        archive.writestr(
            "ml4t_live-1.0.0.dist-info/METADATA",
            "Metadata-Version: 2.4\nName: ml4t-live\nVersion: 1.0.0\n",
        )
    (root / "ml4t_live-1.0.0.tar.gz").write_bytes(b"stable-sdist")


def test_release_inventory_binds_artifacts_commit_and_locked_closure(tmp_path: Path) -> None:
    write_artifacts(tmp_path)

    snapshot, sbom = build_inventories(
        tmp_path,
        commit="a" * 40,
        commit_timestamp="2026-08-09T18:00:00-04:00",
    )

    assert snapshot["version"] == "1.0.0"
    assert snapshot["commit"] == "a" * 40
    assert {artifact["filename"] for artifact in snapshot["artifacts"]} == {
        "ml4t_live-1.0.0-py3-none-any.whl",
        "ml4t_live-1.0.0.tar.gz",
    }
    assert {record["name"] for record in snapshot["packages"]} >= {
        "alpaca-py",
        "ib-async",
        "ml4t-backtest",
        "ml4t-specs",
    }
    assert sbom["bomFormat"] == "CycloneDX"
    assert sbom["specVersion"] == "1.6"
    assert sbom["metadata"]["component"]["bom-ref"] == "pkg:pypi/ml4t-live@1.0.0"
    assert sbom["dependencies"][0]["ref"] == "pkg:pypi/ml4t-live@1.0.0"


def test_release_inventory_is_deterministic(tmp_path: Path) -> None:
    write_artifacts(tmp_path)
    arguments = {
        "commit": "b" * 40,
        "commit_timestamp": "2026-08-09T18:00:00-04:00",
    }

    assert build_inventories(tmp_path, **arguments) == build_inventories(tmp_path, **arguments)
