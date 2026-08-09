"""Fault-sensitive tests for lifecycle qualification."""

from pathlib import Path

import pytest

from scripts.qualification.qualify_lifecycle import (
    EXPECTED_DISTRIBUTIONS,
    QUALIFICATION_TESTS,
    passed_test_count,
)

ROOT = Path(__file__).parents[2]


def test_qualification_uses_only_self_contained_contract_suites() -> None:
    assert len(QUALIFICATION_TESTS) == 4
    assert all((ROOT / path).is_file() for path in QUALIFICATION_TESTS)
    assert set(EXPECTED_DISTRIBUTIONS) == {"ml4t-live", "ml4t-backtest", "ml4t-specs"}


def test_passed_test_count_reads_pytest_summary() -> None:
    assert passed_test_count("79 passed in 10.57s\n") == 79


def test_passed_test_count_rejects_missing_success_summary() -> None:
    with pytest.raises(RuntimeError, match="did not report"):
        passed_test_count("no tests ran in 0.01s\n")
