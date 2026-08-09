"""Tests for the stable security qualification aggregate."""

from scripts.qualification.qualify_security import combined_failures


def test_security_aggregate_accepts_only_complete_passing_reports() -> None:
    reports = {
        "audit": {"passed": True, "failures": []},
        "secrets": {"passed": True, "findings": []},
    }

    assert combined_failures(reports) == []


def test_security_aggregate_retains_every_blocking_control() -> None:
    reports = {
        "audit": {"passed": False, "failures": ["advisory"]},
        "secrets": {"passed": False, "findings": ["credential"]},
    }

    assert combined_failures(reports) == [
        "audit failed with 1 blocking findings",
        "secrets failed with 1 blocking findings",
    ]
