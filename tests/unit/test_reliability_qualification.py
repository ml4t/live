"""Fault-sensitive checks for the reliability qualification runner."""

from pathlib import Path

import pytest

from scripts.qualification.qualify_reliability import (
    PROFILES,
    RELIABILITY_TESTS,
    passed_test_count,
)

ROOT = Path(__file__).parents[2]


def test_profiles_repeat_every_test_with_distinct_seeds_and_orders() -> None:
    assert {version for version, _, _ in PROFILES} == {"3.12", "3.13", "3.14"}
    assert len({seed for _, seed, _ in PROFILES}) == len(PROFILES)
    assert len({order for _, _, order in PROFILES}) == len(PROFILES)
    assert all(set(order) == set(RELIABILITY_TESTS) for _, _, order in PROFILES)
    assert all((ROOT / path).is_file() for path in RELIABILITY_TESTS)


def test_passed_test_count_requires_success_summary() -> None:
    assert passed_test_count("201 passed in 20.1s\n") == 201
    with pytest.raises(RuntimeError, match="did not report"):
        passed_test_count("no tests ran in 0.01s\n")
