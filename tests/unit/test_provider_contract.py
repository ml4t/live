from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.qualification.provider_contract import (
    CURRENT_SCHEMA_VERSION,
    classify_provider_contract,
    provider_contract,
)

PROVIDER_PATHS = {
    "src/ml4t/live/__init__.py",
    "src/ml4t/live/brokers/alpaca.py",
    "src/ml4t/live/brokers/ib.py",
    "src/ml4t/live/feeds/events.py",
    "src/ml4t/live/feeds/okx_feed.py",
    "src/ml4t/live/feeds/queue.py",
    "src/ml4t/live/orders.py",
    "src/ml4t/live/persistence.py",
    "src/ml4t/live/protocols.py",
    "src/ml4t/live/safety.py",
    "src/ml4t/live/state_migration.py",
    "scripts/qualification/qualify_feeds.py",
    "scripts/qualification/qualify_paper.py",
}


def _run(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repository: Path, message: str) -> str:
    _run(repository, "add", ".")
    _run(repository, "commit", "-m", message)
    return _run(repository, "rev-parse", "HEAD")


@pytest.fixture
def contract_repository(tmp_path: Path) -> tuple[Path, str]:
    _run(tmp_path, "init", "--initial-branch=main")
    _run(tmp_path, "config", "user.name", "Contract Test")
    _run(tmp_path, "config", "user.email", "contract@example.test")
    for path in PROVIDER_PATHS:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"contract input: {path}\n")
    (tmp_path / "README.md").write_text("unrelated metadata\n")
    (tmp_path / "pyproject.toml").write_text(
        """[project]
name = "ml4t-live"
requires-python = ">=3.12"
classifiers = [
  "Programming Language :: Python :: 3",
  "Programming Language :: Python :: 3.12",
  "Programming Language :: Python :: 3.13",
  "Programming Language :: Python :: 3.14",
]
"""
    )
    (tmp_path / "uv.lock").write_text(
        """[[package]]
name = "alpaca-py"
version = "0.44.0"

[[package]]
name = "ib-async"
version = "2.1.0"

[[package]]
name = "ccxt"
version = "4.5.78"
"""
    )
    return tmp_path, _commit(tmp_path, "initial contract")


def _classification(
    repository: Path,
    provider: str,
    evidence_commit: str,
    candidate_commit: str,
    *,
    reported_schema: int = CURRENT_SCHEMA_VERSION,
):
    return classify_provider_contract(
        provider,
        evidence_commit=evidence_commit,
        candidate_commit=candidate_commit,
        checkout_root=repository,
        reported_contract=provider_contract(
            provider,
            checkout_root=repository,
            ref=evidence_commit,
            schema_version=reported_schema,
        ),
    )


def test_unrelated_change_preserves_every_provider_contract(
    contract_repository: tuple[Path, str],
) -> None:
    repository, evidence_commit = contract_repository
    (repository / "README.md").write_text("changed metadata\n")
    candidate_commit = _commit(repository, "change unrelated metadata")

    for provider in ("alpaca", "ib", "okx"):
        classification = _classification(repository, provider, evidence_commit, candidate_commit)
        assert classification.reusable is True
        assert classification.reason == "provider contract unchanged"
        assert classification.changed_inputs == ()


def test_shared_persistence_change_invalidates_every_reaching_provider(
    contract_repository: tuple[Path, str],
) -> None:
    repository, evidence_commit = contract_repository
    path = repository / "src/ml4t/live/persistence.py"
    path.write_text(path.read_text() + "changed = True\n")
    candidate_commit = _commit(repository, "change persistence")

    for provider in ("alpaca", "ib", "okx"):
        classification = _classification(repository, provider, evidence_commit, candidate_commit)
        assert classification.reusable is False
        assert classification.reason == "provider contract changed"
        assert classification.changed_inputs == ("src/ml4t/live/persistence.py",)


def test_provider_change_does_not_invalidate_unrelated_provider(
    contract_repository: tuple[Path, str],
) -> None:
    repository, evidence_commit = contract_repository
    path = repository / "src/ml4t/live/feeds/okx_feed.py"
    path.write_text(path.read_text() + "changed = True\n")
    candidate_commit = _commit(repository, "change OKX")

    assert not _classification(repository, "okx", evidence_commit, candidate_commit).reusable
    assert _classification(repository, "ib", evidence_commit, candidate_commit).reusable
    assert _classification(repository, "alpaca", evidence_commit, candidate_commit).reusable


def test_supported_runtime_change_invalidates_every_provider(
    contract_repository: tuple[Path, str],
) -> None:
    repository, evidence_commit = contract_repository
    pyproject = repository / "pyproject.toml"
    pyproject.write_text(pyproject.read_text().replace(">=3.12", ">=3.13"))
    candidate_commit = _commit(repository, "change supported runtime")

    for provider in ("alpaca", "ib", "okx"):
        classification = _classification(repository, provider, evidence_commit, candidate_commit)
        assert classification.reusable is False
        assert classification.changed_inputs == ("supported-runtime",)


def test_older_report_schema_is_upgraded_before_comparison(
    contract_repository: tuple[Path, str],
) -> None:
    repository, evidence_commit = contract_repository
    path = repository / "src/ml4t/live/persistence.py"
    path.write_text(path.read_text() + "changed = True\n")
    candidate_commit = _commit(repository, "change newly covered input")

    classification = _classification(
        repository,
        "ib",
        evidence_commit,
        candidate_commit,
        reported_schema=2,
    )

    assert classification.reusable is False
    assert classification.changed_inputs == ("src/ml4t/live/persistence.py",)


@pytest.mark.parametrize(
    ("reported_contract", "reason"),
    [
        (None, "retained evidence has no provider contract"),
        ({"schema_version": 99}, "retained evidence uses an unsupported contract schema"),
        ({"schema_version": []}, "retained evidence uses an unsupported contract schema"),
    ],
)
def test_unverifiable_contract_fails_closed(
    contract_repository: tuple[Path, str],
    reported_contract: dict | None,
    reason: str,
) -> None:
    repository, commit = contract_repository

    classification = classify_provider_contract(
        "ib",
        evidence_commit=commit,
        candidate_commit=commit,
        checkout_root=repository,
        reported_contract=reported_contract,
    )

    assert classification.reusable is False
    assert classification.reason == reason


def test_tampered_reported_contract_fails_closed(
    contract_repository: tuple[Path, str],
) -> None:
    repository, commit = contract_repository
    contract = provider_contract("ib", checkout_root=repository, ref=commit)

    classification = classify_provider_contract(
        "ib",
        evidence_commit=commit,
        candidate_commit=commit,
        checkout_root=repository,
        reported_contract={**contract, "sha256": "0" * 64},
    )

    assert classification.reusable is False
    assert classification.reason == "reported contract does not match its evidence commit"


def test_ib_tif_change_invalidates_v012_evidence() -> None:
    repository = Path(__file__).resolve().parents[2]
    tag = subprocess.run(
        ["git", "cat-file", "-e", "v0.1.2^{commit}"],
        cwd=repository,
        check=False,
        capture_output=True,
    )
    if tag.returncode != 0:
        pytest.skip("v0.1.2 history is required")
    classification = _classification(repository, "ib", "v0.1.2", "HEAD")

    assert classification.reusable is False
    assert "src/ml4t/live/brokers/ib.py" in classification.changed_inputs
