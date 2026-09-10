"""Fault-sensitive checks for observability qualification."""

from pathlib import Path

import pytest

from scripts.qualification.qualify_observability import (
    OBSERVABILITY_TESTS,
    STRESS_TEST,
    passed_test_count,
)

ROOT = Path(__file__).parents[2]


def test_qualification_inputs_exist_and_include_sustained_diagnostics() -> None:
    assert all((ROOT / path).is_file() for path in OBSERVABILITY_TESTS)
    assert (ROOT / STRESS_TEST).is_file()
    assert STRESS_TEST not in OBSERVABILITY_TESTS


def test_passed_test_count_requires_success_summary() -> None:
    assert passed_test_count("193 passed in 12.4s\n") == 193
    with pytest.raises(RuntimeError, match="did not report"):
        passed_test_count("no tests ran in 0.01s\n")
