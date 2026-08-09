"""Tests for candidate-bound external feed qualification evidence."""

from copy import deepcopy

import pytest

from scripts.qualification.qualify_feeds import (
    REQUIRED_STEPS,
    FeedQualificationError,
    assemble_bundle,
    validate_okx_report,
)

COMMIT = "a" * 40
WHEEL_HASH = "b" * 64


def _candidate() -> dict:
    return {
        "schema_version": 1,
        "repository": "ml4t/live",
        "commit": COMMIT,
        "qualification_run_id": 42,
        "version": "0.1.0b4",
        "wheel": {"filename": "candidate.whl", "sha256": WHEEL_HASH},
        "sdist": {"filename": "candidate.tar.gz", "sha256": "c" * 64},
        "passed": True,
    }


def _report() -> dict:
    return {
        "schema_version": 1,
        "provider": "okx",
        "candidate": {
            "commit": COMMIT,
            "qualification_run_id": 42,
            "version": "0.1.0b4",
            "wheel_sha256": WHEEL_HASH,
        },
        "started_at": "2026-08-09T12:00:00+00:00",
        "completed_at": "2026-08-09T12:02:00+00:00",
        "endpoint": {
            "authentication": "public",
            "host": "www.okx.com",
            "instrument_type": "SWAP",
            "identity_verified": True,
        },
        "steps_passed": sorted(REQUIRED_STEPS),
        "event_kinds": ["bar", "funding"],
        "complete_interval_seconds": 60,
        "native_comparison_exact": True,
        "reconnect_continuity": True,
        "stale_rejected": True,
        "overload": {"failed_closed": True, "overflow_count": 1, "retained_occupancy": 0},
        "maximum_shutdown_seconds": 0.1,
        "passed": True,
    }


def test_bundle_binds_okx_evidence_and_requires_all_other_feeds_to_opt_in() -> None:
    bundle = assemble_bundle(_candidate(), _report())

    assert bundle["candidate"]["wheel_sha256"] == WHEEL_HASH
    assert bundle["stable_feeds"] == [
        {
            "feed": "OKXFundingFeed",
            "provider": "okx",
            "external_evidence": True,
            "passed": True,
        }
    ]
    assert {item["feed"] for item in bundle["experimental_feeds"]} == {
        "AlpacaDataFeed",
        "IBDataFeed",
        "DataBentoFeed",
        "CryptoFeed",
    }
    assert all(item["explicit_opt_in_required"] for item in bundle["experimental_feeds"])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("candidate", {"commit": "d" * 40}),
        ("steps_passed", ["connect"]),
        ("native_comparison_exact", False),
        ("reconnect_continuity", False),
        ("stale_rejected", False),
        ("maximum_shutdown_seconds", 5.0),
        ("passed", False),
    ],
)
def test_okx_report_rejects_incomplete_or_wrong_candidate_evidence(field: str, value) -> None:
    report = deepcopy(_report())
    report[field] = value

    with pytest.raises(FeedQualificationError):
        validate_okx_report(report, _candidate())


def test_okx_report_rejects_non_fail_closed_overload() -> None:
    report = deepcopy(_report())
    report["overload"]["failed_closed"] = False

    with pytest.raises(FeedQualificationError):
        validate_okx_report(report, _candidate())
