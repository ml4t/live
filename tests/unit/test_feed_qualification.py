"""Tests for candidate-bound external feed qualification evidence."""

import json
import sys
from copy import deepcopy
from datetime import UTC, datetime

import httpx
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


@pytest.mark.parametrize("connection_failure", [False, True])
def test_okx_soak_cli_retains_report_when_native_request_failure_blocks_qualification(
    monkeypatch, tmp_path, connection_failure: bool
) -> None:
    from scripts.qualification import qualify_feeds, qualify_provider_soaks

    now_ms = int(datetime.now(UTC).timestamp() * 1_000)
    candle_ms = now_ms // 60_000 * 60_000 - 60_000
    failed_requests = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal failed_requests
        if request.url.path.endswith("/candles"):
            if connection_failure and failed_requests == 0:
                failed_requests += 1
                raise httpx.ConnectError("All connection attempts failed", request=request)
            records = [[str(candle_ms), "100", "102", "99", "101", "12", "0", "0", "1"]]
        elif request.url.path.endswith("/funding-rate"):
            records = [
                {"fundingTime": str(now_ms + 3_600_000), "ts": str(now_ms), "fundingRate": "0.0001"}
            ]
        else:
            records = [{"instId": "BTC-USDT-SWAP", "instType": "SWAP"}]
        return httpx.Response(200, json={"code": "0", "data": records})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs),
    )
    monkeypatch.setattr(qualify_feeds, "SOAK_DURATION_SECONDS", 2)
    monkeypatch.setattr(qualify_feeds, "SOAK_SNAPSHOT_INTERVAL_SECONDS", 0.5)
    monkeypatch.setattr(qualify_feeds, "_verify_installed_candidate", lambda *args: None)
    monkeypatch.setattr(qualify_feeds, "provider_contract", lambda *args, **kwargs: {})
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps(_candidate()))
    output = tmp_path / "okx-soak.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "qualify_provider_soaks",
            "--candidate",
            str(candidate),
            "--checkout-root",
            str(tmp_path),
            "--provider",
            "okx",
            "--alpaca-output",
            str(tmp_path / "alpaca.json"),
            "--ib-output",
            str(tmp_path / "ib.json"),
            "--okx-output",
            str(output),
        ],
    )

    result = qualify_provider_soaks.main()

    assert failed_requests == int(connection_failure)
    assert result == int(connection_failure)
    assert output.exists(), "A rejected native feed session must retain its actual soak metrics"
    report = json.loads(output.read_text())
    assert report["passed"] is (not connection_failure)
    assert report["error_count"] == int(connection_failure)
    assert report["reconnect_count"] == 1
    assert report["native_final_reconciliation"] is True
    assert report["complete_bar_count"] >= 1
    assert report["funding_count"] >= 1
    if connection_failure:
        with pytest.raises(FeedQualificationError, match="stable provider contract"):
            validate_soak_report(report, _candidate())
    else:
        validate_soak_report(report, _candidate())


@pytest.mark.asyncio
async def test_native_funding_comparison_uses_observation_identity_and_rate() -> None:
    from ml4t.live import OKXFundingFeed
    from scripts.qualification.qualify_feeds import _native_matches

    now_ms = int(datetime.now(UTC).timestamp() * 1_000)
    candle = [str(now_ms - 60_000), "100", "102", "99", "101", "12", "0", "0", "1"]
    native = {
        "candles": [candle],
        "funding": [
            {"fundingTime": str(now_ms + 3_600_000), "ts": str(now_ms), "fundingRate": "0.0001"}
        ],
    }

    def respond(request: httpx.Request) -> httpx.Response:
        records = native["candles"] if request.url.path.endswith("/candles") else native["funding"]
        return httpx.Response(200, json={"code": "0", "data": records})

    feed = OKXFundingFeed(["BTC-USDT-SWAP"], timeframe="1m")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        feed._client = client
        await feed._fetch_and_emit()
        events = [await anext(feed), await anext(feed)]
    assert _native_matches(events, native)
    incorrect = deepcopy(native)
    incorrect["funding"][0]["fundingRate"] = "0.01"
    assert not _native_matches(events, incorrect)


def _candidate() -> dict:
    return {
        "schema_version": 1,
        "repository": "ml4t/live",
        "commit": COMMIT,
        "qualification_run_id": 42,
        "version": "0.1.0",
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
            "version": "0.1.0",
            "wheel_sha256": WHEEL_HASH,
            "sdist_sha256": "c" * 64,
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
