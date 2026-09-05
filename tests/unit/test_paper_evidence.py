from __future__ import annotations

import io
import json
import subprocess
import urllib.request
import zipfile
from pathlib import Path

import pytest

from scripts.qualification.check_paper_evidence import (
    _ArtifactRedirectHandler,
    fetch_bytes,
    find_paper_evidence,
)
from scripts.qualification.provider_contract import (
    provider_contract,
    provider_contract_matches,
)
from scripts.qualification.qualify_feeds import REQUIRED_STEPS
from scripts.qualification.qualify_paper import (
    EXERCISE_STEPS,
    RESTART_STEPS,
    SOAK_DURATION_SECONDS,
    SOAK_SNAPSHOT_INTERVAL_SECONDS,
)

COMMIT = "a" * 40
LEGACY_COMMIT = "98c414e9d858427c31e1680faccdc8dca498bf6b"


def _fetcher(
    created_at: str,
    *,
    conclusion: str = "success",
    expired: bool = False,
    release: bool = False,
):
    run = {
        "id": 42,
        "head_sha": COMMIT,
        "conclusion": conclusion,
        "status": "completed",
        "created_at": created_at,
        "artifacts_url": "https://api.github.test/artifacts",
        "html_url": "https://github.test/run/42",
    }

    def fetch(url: str, token: str) -> dict:
        assert token == "token"
        if url.endswith("/artifacts"):
            return {
                "artifacts": [
                    {
                        "name": f"paper-{COMMIT}-42",
                        "expired": expired,
                        "created_at": created_at,
                        "archive_download_url": "https://api.github.test/archive/42",
                    }
                ]
            }
        if "/releases/tags/" in url:
            return {
                "assets": (
                    [
                        {
                            "name": "provider-evidence.zip",
                            "url": "https://api.github.test/release-asset/42",
                        }
                    ]
                    if release
                    else []
                )
            }
        return {"workflow_runs": [run]}

    return fetch


def _snapshot() -> dict:
    return {
        "positions_count": 0,
        "pending_orders_count": 0,
        "filtered_pending_orders_count": 0,
        "position_snapshot_exact": True,
        "pending_order_snapshot_exact": True,
        "account_value_valid": True,
        "cash_valid": True,
    }


def _report(provider: str, phase: str, commit: str) -> dict:
    snapshots = (
        {"initial": _snapshot(), "reconnect": _snapshot(), "final": _snapshot()}
        if phase == "exercise"
        else {"restart": _snapshot()}
    )
    return {
        "schema_version": 1,
        "provider": provider,
        "phase": phase,
        "candidate": {
            "commit": commit,
            "qualification_run_id": 41,
            "version": "0.1.0",
            "wheel_sha256": "b" * 64,
            "sdist_sha256": "c" * 64,
        },
        "started_at": "2026-08-07T20:00:00+00:00",
        "completed_at": "2026-08-07T20:01:00+00:00",
        "paper_identity_verified_before_submission": True,
        "steps_passed": sorted(EXERCISE_STEPS if phase == "exercise" else RESTART_STEPS),
        "snapshots": snapshots,
        "cleanup_passed": True,
        "failed_stage": None,
        "passed": True,
    }


def _soak_report(provider: str, commit: str) -> dict:
    snapshots = [
        {
            "elapsed_seconds": index * SOAK_SNAPSHOT_INTERVAL_SECONDS,
            "rss_bytes": 100_000_000,
            "positions_count": 0,
            "pending_orders_count": 0,
            "filtered_pending_orders_count": 0,
            "position_snapshot_exact": True,
            "pending_order_snapshot_exact": True,
            "account_value_valid": True,
            "cash_valid": True,
            "connected": True,
        }
        for index in range(SOAK_DURATION_SECONDS // SOAK_SNAPSHOT_INTERVAL_SECONDS + 1)
    ]
    return {
        "schema_version": 1,
        "provider": provider,
        "candidate": {
            "commit": commit,
            "qualification_run_id": 41,
            "version": "0.1.0",
            "wheel_sha256": "b" * 64,
            "sdist_sha256": "c" * 64,
        },
        "started_at": "2026-08-07T20:00:00+00:00",
        "completed_at": "2026-08-08T02:00:01+00:00",
        "duration_seconds": SOAK_DURATION_SECONDS + 0.1,
        "snapshot_interval_seconds": SOAK_SNAPSHOT_INTERVAL_SECONDS,
        "snapshots": snapshots,
        "paper_identity_verified": True,
        "reconnect_count": 1,
        "unexpected_disconnect_count": 0,
        "continuity_gap_count": 0,
        "initial_state_checksum": "d" * 64,
        "final_state_checksum": "d" * 64,
        "final_reconciliation_exact": True,
        "state_unchanged": True,
        "rss_growth_bytes": 0,
        "maximum_shutdown_seconds": 0.1,
        "error_count": 0,
        "failed_stage": None,
        "failure_type": None,
        "passed": True,
    }


def _manifest(commit: str) -> dict:
    return {
        "schema_version": 1,
        "repository": "ml4t/live",
        "commit": commit,
        "qualification_run_id": 41,
        "version": "0.1.0",
        "wheel": {"filename": "ml4t_live-0.1.0-py3-none-any.whl", "sha256": "b" * 64},
        "sdist": {"filename": "ml4t_live-0.1.0.tar.gz", "sha256": "c" * 64},
        "passed": True,
    }


def _okx_report(commit: str) -> dict:
    return {
        "schema_version": 1,
        "provider": "okx",
        "candidate": _soak_report("alpaca", commit)["candidate"],
        "started_at": "2026-08-07T20:00:00+00:00",
        "completed_at": "2026-08-07T20:02:00+00:00",
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


def _okx_soak_report(commit: str) -> dict:
    report = {
        "schema_version": 1,
        "provider": "okx",
        "candidate": _soak_report("alpaca", commit)["candidate"],
        "started_at": "2026-08-07T20:00:00+00:00",
        "completed_at": "2026-08-08T02:00:01+00:00",
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
    return report


def _archive(commit: str = COMMIT) -> bytes:
    bundle = {
        "schema_version": 1,
        "generated_at": "2026-08-08T02:01:00+00:00",
        "candidate": {
            "commit": commit,
            "qualification_run_id": 41,
            "version": "0.1.0",
            "wheel_sha256": "b" * 64,
            "sdist_sha256": "c" * 64,
        },
        "providers": {
            provider: {phase: _report(provider, phase, commit) for phase in ("exercise", "restart")}
            for provider in ("alpaca", "ib")
        },
        "soaks": {provider: _soak_report(provider, commit) for provider in ("alpaca", "ib")},
        "redacted": True,
        "passed": True,
    }
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("paper-evidence/candidate.json", json.dumps(_manifest(commit)))
        for provider in ("alpaca", "ib"):
            for phase in ("exercise", "restart"):
                archive.writestr(
                    f"paper-evidence/{provider}-{phase}.json",
                    json.dumps(_report(provider, phase, commit)),
                )
            archive.writestr(
                f"paper-evidence/{provider}-soak.json",
                json.dumps(_soak_report(provider, commit)),
            )
        archive.writestr("feed-evidence/okx.json", json.dumps(_okx_report(commit)))
        archive.writestr("feed-evidence/okx-soak.json", json.dumps(_okx_soak_report(commit)))
        archive.writestr("paper-qualification.json", json.dumps(bundle))
        archive.writestr(
            "feed-qualification.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "candidate": bundle["candidate"],
                    "generated_at": "2026-08-07T20:02:00+00:00",
                    "stable_feeds": [
                        {
                            "feed": "OKXFundingFeed",
                            "provider": "okx",
                            "external_evidence": True,
                            "continuous_session_seconds": 21_600.1,
                            "reconnect_count": 1,
                            "passed": True,
                        }
                    ],
                    "experimental_feeds": [
                        {
                            "feed": name,
                            "status": "experimental",
                            "explicit_opt_in_required": True,
                            "missing_guarantees": ["not qualified"],
                        }
                        for name in (
                            "AlpacaDataFeed",
                            "IBDataFeed",
                            "DataBentoFeed",
                            "CryptoFeed",
                        )
                    ],
                    "passed": True,
                }
            ),
        )
    return payload.getvalue()


def _downloader(
    payload: bytes = _archive(),
    expected_url: str = "https://api.github.test/archive/42",
):
    def download(url: str, token: str) -> bytes:
        assert url == expected_url
        assert token == "token"
        return payload

    return download


def test_artifact_download_uses_github_json_media_type(monkeypatch) -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return b"artifact"

    class Opener:
        def open(self, request, *, timeout: int):
            assert request.get_header("Accept") == "application/octet-stream"
            assert timeout == 30
            return Response()

    def build_opener(handler):
        assert isinstance(handler, _ArtifactRedirectHandler)
        return Opener()

    monkeypatch.setattr("urllib.request.build_opener", build_opener)

    assert fetch_bytes("https://api.github.test/artifact.zip", "token") == b"artifact"


def test_artifact_redirect_drops_github_credentials_on_host_change() -> None:
    request = urllib.request.Request(
        "https://api.github.com/repos/ml4t/live/actions/artifacts/42/zip",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer token",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )

    redirected = _ArtifactRedirectHandler().redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://signed-results-receiver.example/artifact.zip",
    )

    assert redirected is not None
    assert redirected.get_header("Authorization") is None
    assert redirected.get_header("X-GitHub-Api-Version") is None
    assert redirected.get_header("Accept") is None


def test_legacy_ib_soak_remains_valid_while_changed_providers_do_not() -> None:
    repository = Path(__file__).resolve().parents[2]
    candidate = "HEAD"
    history_available = subprocess.run(
        ["git", "cat-file", "-e", f"{LEGACY_COMMIT}^{{commit}}"],
        cwd=repository,
        check=False,
        capture_output=True,
    )
    if history_available.returncode != 0:
        pytest.skip("legacy provider evidence check requires the repository's full Git history")

    assert provider_contract_matches(
        "ib",
        evidence_commit=LEGACY_COMMIT,
        candidate_commit=candidate,
        checkout_root=repository,
        reported_contract=None,
    )
    for provider in ("alpaca", "okx"):
        assert not provider_contract_matches(
            provider,
            evidence_commit=LEGACY_COMMIT,
            candidate_commit=candidate,
            checkout_root=repository,
            reported_contract=None,
        )


def test_reported_provider_contract_must_match_its_source_revision() -> None:
    repository = Path(__file__).resolve().parents[2]
    contract = provider_contract("ib", checkout_root=repository)

    assert provider_contract_matches(
        "ib",
        evidence_commit="HEAD",
        candidate_commit="HEAD",
        checkout_root=repository,
        reported_contract=contract,
    )
    assert not provider_contract_matches(
        "ib",
        evidence_commit="HEAD",
        candidate_commit="HEAD",
        checkout_root=repository,
        reported_contract={**contract, "sha256": "0" * 64},
    )


def test_exact_candidate_and_matching_provider_evidence_passes() -> None:
    evidence = find_paper_evidence(
        repository="ml4t/live",
        commit=COMMIT,
        token="token",
        checkout_root=Path("."),
        fetcher=_fetcher("2026-08-07T20:00:00Z"),
        downloader=_downloader(),
        contract_matcher=lambda *_args, **_kwargs: True,
    )

    assert evidence is not None
    assert {key: value for key, value in evidence.items() if key != "providers"} == {
        "run_id": 42,
        "run_url": "https://github.test/run/42",
        "created_at": "2026-08-07T20:00:00Z",
        "artifact": f"paper-{COMMIT}-42",
        "qualification_run_id": 41,
        "wheel_sha256": "b" * 64,
        "sdist_sha256": "c" * 64,
    }
    assert set(evidence["providers"]) == {"alpaca", "ib", "okx"}


def test_age_does_not_invalidate_matching_provider_evidence() -> None:
    evidence = find_paper_evidence(
        repository="ml4t/live",
        commit=COMMIT,
        token="token",
        checkout_root=Path("."),
        fetcher=_fetcher("2026-07-01T20:00:00Z"),
        downloader=_downloader(),
        contract_matcher=lambda *_args, **_kwargs: True,
    )

    assert evidence is not None


def test_changed_provider_contract_fails_closed() -> None:
    evidence = find_paper_evidence(
        repository="ml4t/live",
        commit=COMMIT,
        token="token",
        checkout_root=Path("."),
        fetcher=_fetcher("2026-08-07T20:00:00Z"),
        downloader=_downloader(),
        contract_matcher=lambda provider, **_kwargs: provider != "ib",
    )

    assert evidence is None


def test_expired_paper_artifact_fails() -> None:
    evidence = find_paper_evidence(
        repository="ml4t/live",
        commit=COMMIT,
        token="token",
        checkout_root=Path("."),
        fetcher=_fetcher("2026-08-07T20:00:00Z", expired=True),
        downloader=_downloader(),
        contract_matcher=lambda *_args, **_kwargs: True,
    )

    assert evidence is None


def test_durable_release_asset_replaces_an_expired_actions_artifact() -> None:
    evidence = find_paper_evidence(
        repository="ml4t/live",
        commit=COMMIT,
        token="token",
        checkout_root=Path("."),
        fetcher=_fetcher("2026-08-07T20:00:00Z", expired=True, release=True),
        downloader=_downloader(expected_url="https://api.github.test/release-asset/42"),
        contract_matcher=lambda *_args, **_kwargs: True,
    )

    assert evidence is not None
    assert evidence["artifact"] == "provider-evidence-42/provider-evidence.zip"


def test_wrong_commit_inside_retained_artifact_fails() -> None:
    evidence = find_paper_evidence(
        repository="ml4t/live",
        commit=COMMIT,
        token="token",
        checkout_root=Path("."),
        fetcher=_fetcher("2026-08-07T20:00:00Z"),
        downloader=_downloader(_archive("c" * 40)),
        contract_matcher=lambda *_args, **_kwargs: True,
    )

    assert evidence is None


def test_malformed_retained_artifact_fails() -> None:
    evidence = find_paper_evidence(
        repository="ml4t/live",
        commit=COMMIT,
        token="token",
        checkout_root=Path("."),
        fetcher=_fetcher("2026-08-07T20:00:00Z"),
        downloader=_downloader(b"not a zip"),
        contract_matcher=lambda *_args, **_kwargs: True,
    )

    assert evidence is None


def test_missing_feed_qualification_fails() -> None:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("paper-qualification.json", "{}")

    evidence = find_paper_evidence(
        repository="ml4t/live",
        commit=COMMIT,
        token="token",
        checkout_root=Path("."),
        fetcher=_fetcher("2026-08-07T20:00:00Z"),
        downloader=_downloader(payload.getvalue()),
        contract_matcher=lambda *_args, **_kwargs: True,
    )

    assert evidence is None
