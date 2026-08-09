"""Fault-sensitive checks for observability qualification."""

from pathlib import Path

import pytest

from scripts.qualification.qualify_observability import (
    OBSERVABILITY_TESTS,
    STRESS_TEST,
    passed_test_count,
)
from scripts.qualification.scan_release_secrets import _default_evidence_root

ROOT = Path(__file__).parents[2]


def test_qualification_inputs_exist_and_include_sustained_diagnostics() -> None:
    assert all((ROOT / path).is_file() for path in OBSERVABILITY_TESTS)
    assert (ROOT / STRESS_TEST).is_file()
    assert STRESS_TEST not in OBSERVABILITY_TESTS


def test_secret_scan_defaults_to_stable_evidence(tmp_path) -> None:
    repository = tmp_path / "ml4t-live"
    evidence = tmp_path / "ml4t-live-dev" / ".workspace" / "work" / "ml4t-live-stable-readiness"
    repository.mkdir()
    evidence.mkdir(parents=True)

    assert _default_evidence_root(repository) == evidence


def test_passed_test_count_requires_success_summary() -> None:
    assert passed_test_count("193 passed in 12.4s\n") == 193
    with pytest.raises(RuntimeError, match="did not report"):
        passed_test_count("no tests ran in 0.01s\n")
