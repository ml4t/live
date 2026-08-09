from __future__ import annotations

import email
import io
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

from scripts.qualification import qualify_artifacts
from scripts.qualification.qualify_artifacts import (
    EXPECTED_CLASSIFIERS,
    EXPECTED_URLS,
    QualificationError,
    load_expected_manifests,
    normalized_sdist_manifest,
    normalized_wheel_manifest,
    qualify_install_profiles,
    validate_metadata,
)
from scripts.qualification.scan_release_secrets import scan_payloads

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_release_manifest_marks_typing_and_excludes_agent_files() -> None:
    wheel, sdist = load_expected_manifests()

    assert "ml4t/live/py.typed" in wheel
    assert "src/ml4t/live/py.typed" in sdist
    assert not any("AGENTS.md" in path or ".workspace" in path for path in wheel | sdist)


def test_artifact_manifest_normalization(tmp_path: Path) -> None:
    wheel_path = tmp_path / "package.whl"
    with zipfile.ZipFile(wheel_path, "w") as archive:
        archive.writestr("ml4t/live/py.typed", b"")
        archive.writestr("ml4t_live-0.1.0b4.dist-info/METADATA", b"metadata")

    sdist_path = tmp_path / "package.tar.gz"
    with tarfile.open(sdist_path, "w:gz") as archive:
        payload = b"source"
        member = tarfile.TarInfo("ml4t_live-0.1.0b4/src/ml4t/live/__init__.py")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))

    assert normalized_wheel_manifest(wheel_path) == {
        "ml4t/live/py.typed",
        "<dist-info>/METADATA",
    }
    assert normalized_sdist_manifest(sdist_path) == {"src/ml4t/live/__init__.py"}


def test_metadata_contract_accepts_declared_beta_candidate() -> None:
    with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]
    message = email.message.Message()
    message["Name"] = "ml4t-live"
    message["Version"] = "0.1.0b4"
    message["Requires-Python"] = ">=3.12,<3.15"
    message["License-Expression"] = "MIT"
    for classifier in EXPECTED_CLASSIFIERS:
        message["Classifier"] = classifier
    for label, url in EXPECTED_URLS.items():
        message["Project-URL"] = f"{label}, {url}"
    for dependency in project["dependencies"]:
        message["Requires-Dist"] = dependency

    assert validate_metadata(message, project) == "0.1.0b4"


def test_metadata_contract_rejects_unbounded_python() -> None:
    message = email.message.Message()
    message["Name"] = "ml4t-live"
    message["Version"] = "0.1.0b4"
    message["Requires-Python"] = ">=3.12"
    message["License-Expression"] = "MIT"

    with pytest.raises(QualificationError, match="Requires-Python"):
        validate_metadata(message, {"dependencies": []})


def test_secret_scan_reports_only_redacted_location() -> None:
    payload = b"key=" + b"AKIA" + (b"A" * 16)

    result = scan_payloads((("private/location", payload),))

    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.pattern == "aws-access-key"
    assert finding.location_digest != "private/location"
    assert "AKIA" not in repr(finding)


def test_install_matrix_continues_after_profile_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel = tmp_path / "candidate.whl"
    sdist = tmp_path / "candidate.tar.gz"
    wheel.touch()
    sdist.touch()
    calls: list[tuple[str, str]] = []

    def fake_install(artifact: Path, python: str, expected: str, root: Path) -> None:
        calls.append((artifact.suffix, python))
        if len(calls) == 1:
            raise QualificationError("seeded profile failure")

    monkeypatch.setattr(qualify_artifacts, "install_profile", fake_install)

    results = qualify_install_profiles((wheel, sdist), "0.1.0b4", tmp_path / "profiles")

    assert len(calls) == 6
    assert len(results) == 6
    assert not results[0].passed
    assert all(result.passed for result in results[1:])
