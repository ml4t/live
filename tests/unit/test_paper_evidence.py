from __future__ import annotations

import io
import json
import urllib.request
import zipfile
from datetime import UTC, datetime, timedelta

from scripts.qualification.check_paper_evidence import (
    _ArtifactRedirectHandler,
    fetch_bytes,
    find_fresh_paper_run,
)
from scripts.qualification.qualify_paper import EXERCISE_STEPS, RESTART_STEPS

NOW = datetime(2026, 8, 8, 20, tzinfo=UTC)
COMMIT = "a" * 40


def _fetcher(created_at: str, *, conclusion: str = "success", expired: bool = False):
    run = {
        "id": 42,
        "head_sha": COMMIT,
        "conclusion": conclusion,
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
            "version": "0.1.0b4",
            "wheel_sha256": "b" * 64,
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


def _archive(commit: str = COMMIT) -> bytes:
    bundle = {
        "schema_version": 1,
        "generated_at": "2026-08-07T20:02:00+00:00",
        "candidate": {
            "commit": commit,
            "qualification_run_id": 41,
            "version": "0.1.0b4",
            "wheel_sha256": "b" * 64,
        },
        "providers": {
            provider: {phase: _report(provider, phase, commit) for phase in ("exercise", "restart")}
            for provider in ("alpaca", "ib")
        },
        "redacted": True,
        "passed": True,
    }
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
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


def _downloader(payload: bytes = _archive()):
    def download(url: str, token: str) -> bytes:
        assert url == "https://api.github.test/archive/42"
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
            assert request.get_header("Accept") == "application/vnd.github+json"
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


def test_exact_fresh_successful_paper_evidence_passes() -> None:
    evidence = find_fresh_paper_run(
        repository="ml4t/live",
        commit=COMMIT,
        token="token",
        max_age=timedelta(days=7),
        now=NOW,
        fetcher=_fetcher("2026-08-07T20:00:00Z"),
        downloader=_downloader(),
    )

    assert evidence == {
        "run_id": 42,
        "run_url": "https://github.test/run/42",
        "created_at": "2026-08-07T20:00:00Z",
        "artifact": f"paper-{COMMIT}-42",
        "qualification_run_id": 41,
        "wheel_sha256": "b" * 64,
    }


def test_stale_paper_evidence_fails() -> None:
    evidence = find_fresh_paper_run(
        repository="ml4t/live",
        commit=COMMIT,
        token="token",
        max_age=timedelta(days=7),
        now=NOW,
        fetcher=_fetcher("2026-07-01T20:00:00Z"),
        downloader=_downloader(),
    )

    assert evidence is None


def test_expired_paper_artifact_fails() -> None:
    evidence = find_fresh_paper_run(
        repository="ml4t/live",
        commit=COMMIT,
        token="token",
        max_age=timedelta(days=7),
        now=NOW,
        fetcher=_fetcher("2026-08-07T20:00:00Z", expired=True),
        downloader=_downloader(),
    )

    assert evidence is None


def test_wrong_commit_inside_retained_artifact_fails() -> None:
    evidence = find_fresh_paper_run(
        repository="ml4t/live",
        commit=COMMIT,
        token="token",
        max_age=timedelta(days=7),
        now=NOW,
        fetcher=_fetcher("2026-08-07T20:00:00Z"),
        downloader=_downloader(_archive("c" * 40)),
    )

    assert evidence is None


def test_malformed_retained_artifact_fails() -> None:
    evidence = find_fresh_paper_run(
        repository="ml4t/live",
        commit=COMMIT,
        token="token",
        max_age=timedelta(days=7),
        now=NOW,
        fetcher=_fetcher("2026-08-07T20:00:00Z"),
        downloader=_downloader(b"not a zip"),
    )

    assert evidence is None


def test_missing_feed_qualification_fails() -> None:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("paper-qualification.json", "{}")

    evidence = find_fresh_paper_run(
        repository="ml4t/live",
        commit=COMMIT,
        token="token",
        max_age=timedelta(days=7),
        now=NOW,
        fetcher=_fetcher("2026-08-07T20:00:00Z"),
        downloader=_downloader(payload.getvalue()),
    )

    assert evidence is None
