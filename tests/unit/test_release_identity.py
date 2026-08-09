"""Adversarial tests for stable release identity verification."""

from __future__ import annotations

import json
import tarfile
import zipfile
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.qualification.verify_release_identity import identity_failures, sha256

COMMIT = "a" * 40


def release_fixture(tmp_path: Path) -> dict:
    """Create one internally consistent release identity."""
    wheel = tmp_path / "ml4t_live-1.2.3-py3-none-any.whl"
    sdist = tmp_path / "ml4t_live-1.2.3.tar.gz"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "ml4t_live-1.2.3.dist-info/METADATA",
            "Metadata-Version: 2.4\nName: ml4t-live\nVersion: 1.2.3\n",
        )
    with tarfile.open(sdist, "w:gz"):
        pass
    artifacts = {
        "wheel": {"filename": wheel.name, "sha256": sha256(wheel)},
        "sdist": {"filename": sdist.name, "sha256": sha256(sdist)},
    }
    listed = list(artifacts.values())
    snapshot = {"commit": COMMIT, "version": "1.2.3", "artifacts": listed}
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "metadata": {
            "component": {
                "version": "1.2.3",
                "properties": [
                    {"name": "ml4t:source:commit", "value": COMMIT},
                    *(
                        {
                            "name": f"ml4t:artifact:{record['filename']}:sha256",
                            "value": record["sha256"],
                        }
                        for record in listed
                    ),
                ],
            }
        },
    }
    sbom_path = tmp_path / "sbom.json"
    snapshot_path = tmp_path / "snapshot.json"
    sbom_path.write_text(json.dumps(sbom))
    snapshot_path.write_text(json.dumps(snapshot))
    return {
        "artifacts_directory": tmp_path,
        "artifact_report": {
            "passed": True,
            "commit": COMMIT,
            "version": "1.2.3",
            "dirty_candidate": False,
            "reproducible": True,
            "manifests_exact": True,
            "artifacts": artifacts,
            "profiles": [{"passed": True}],
        },
        "security_report": {
            "passed": True,
            "commit": COMMIT,
            "version": "1.2.3",
            "artifacts": listed,
            "retained_reports": {
                "sbom.cdx.json": sha256(sbom_path),
                "dependency-snapshot.json": sha256(snapshot_path),
            },
        },
        "sbom": sbom,
        "dependency_snapshot": snapshot,
        "commit": COMMIT,
        "tag": "v1.2.3",
        "paper_wheel_sha256": artifacts["wheel"]["sha256"],
        "paper_sdist_sha256": artifacts["sdist"]["sha256"],
        "sbom_sha256": sha256(sbom_path),
        "dependency_snapshot_sha256": sha256(snapshot_path),
    }


def test_consistent_stable_release_identity_passes(tmp_path: Path) -> None:
    assert identity_failures(**release_fixture(tmp_path)) == []


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ("tag", "release tag"),
        ("commit", "release commit"),
        ("paper-wheel", "paper evidence wheel"),
        ("paper-sdist", "paper evidence sdist"),
        ("artifact", "artifact qualification"),
        ("security", "security qualification"),
        ("snapshot", "dependency snapshot"),
        ("sbom", "SBOM identity"),
    ),
)
def test_every_release_identity_mismatch_blocks_publication(
    tmp_path: Path, mutation: str, expected: str
) -> None:
    inputs = release_fixture(tmp_path)
    seeded = deepcopy(inputs)
    if mutation == "tag":
        seeded["tag"] = "v1.2.4"
    elif mutation == "commit":
        seeded["commit"] = "short"
    elif mutation == "paper-wheel":
        seeded["paper_wheel_sha256"] = "b" * 64
    elif mutation == "paper-sdist":
        seeded["paper_sdist_sha256"] = "b" * 64
    elif mutation == "artifact":
        seeded["artifact_report"]["passed"] = False
    elif mutation == "security":
        seeded["security_report"]["commit"] = "c" * 40
    elif mutation == "snapshot":
        seeded["dependency_snapshot"]["version"] = "1.2.4"
    else:
        seeded["sbom"]["specVersion"] = "1.5"

    assert any(expected in failure for failure in identity_failures(**seeded))
