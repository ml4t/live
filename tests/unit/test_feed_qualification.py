"""Tests for candidate-bound external feed qualification evidence."""

from copy import deepcopy

import pytest

from scripts.qualification.qualify_feeds import (
    REQUIRED_STEPS,
    FeedQualificationError,
    assemble_bundle,
    validate_okx_report,
    validate_soak_report,
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


def _soak_report() -> dict:
    return {
        "schema_version": 1,
        "provider": "okx",
        "candidate": _report()["candidate"],
        "started_at": "2026-08-09T12:00:00+00:00",
        "completed_at": "2026-08-09T18:00:01+00:00",
        "duration_seconds": 21_600.1,
        "snapshot_interval_seconds": 300,
        "snapshots": [
            {
                "elapsed_seconds": index * 300,
                "rss_bytes": 100_000_000,
                "event_count": index + 1,
                "complete_bar_count": index + 1,
                "funding_count": 1,
                "error_count": 0,
                "rejected_count": 0,
                "overflow_count": 0,
                "queue_high_watermark": 2,
            }
            for index in range(73)
        ],
        "event_count": 361,
        "complete_bar_count": 360,
        "funding_count": 1,
        "event_checksum": "d" * 64,
        "reconnect_count": 1,
        "continuity_gap_count": 0,
        "native_final_reconciliation": True,
        "rss_growth_bytes": 0,
        "maximum_shutdown_seconds": 0.1,
        "error_count": 0,
        "rejected_count": 0,
        "overflow_count": 0,
        "passed": True,
    }


def test_bundle_binds_okx_evidence_and_requires_all_other_feeds_to_opt_in() -> None:
    bundle = assemble_bundle(_candidate(), _report(), _soak_report())

    assert bundle["candidate"]["wheel_sha256"] == WHEEL_HASH
    assert bundle["stable_feeds"] == [
        {
            "feed": "OKXFundingFeed",
            "provider": "okx",
            "external_evidence": True,
            "continuous_session_seconds": 21_600.1,
            "reconnect_count": 1,
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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("duration_seconds", 21_599.9),
        ("reconnect_count", 0),
        ("continuity_gap_count", 1),
        ("native_final_reconciliation", False),
        ("rss_growth_bytes", 25 * 1024 * 1024),
        ("maximum_shutdown_seconds", 5.0),
        ("error_count", 1),
        ("rejected_count", 1),
        ("overflow_count", 1),
        ("passed", False),
    ],
)
def test_soak_report_rejects_any_stable_provider_failure(field: str, value) -> None:
    report = deepcopy(_soak_report())
    report[field] = value

    with pytest.raises(FeedQualificationError):
        validate_soak_report(report, _candidate())
