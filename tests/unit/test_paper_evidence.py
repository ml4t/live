from __future__ import annotations

from datetime import UTC, datetime, timedelta

from scripts.qualification.check_paper_evidence import find_fresh_paper_run

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
                    }
                ]
            }
        return {"workflow_runs": [run]}

    return fetch


def test_exact_fresh_successful_paper_evidence_passes() -> None:
    evidence = find_fresh_paper_run(
        repository="ml4t/live",
        commit=COMMIT,
        token="token",
        max_age=timedelta(days=7),
        now=NOW,
        fetcher=_fetcher("2026-08-07T20:00:00Z"),
    )

    assert evidence == {
        "run_id": 42,
        "run_url": "https://github.test/run/42",
        "created_at": "2026-08-07T20:00:00Z",
        "artifact": f"paper-{COMMIT}-42",
    }


def test_stale_paper_evidence_fails() -> None:
    evidence = find_fresh_paper_run(
        repository="ml4t/live",
        commit=COMMIT,
        token="token",
        max_age=timedelta(days=7),
        now=NOW,
        fetcher=_fetcher("2026-07-01T20:00:00Z"),
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
    )

    assert evidence is None
