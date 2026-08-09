"""Require fresh successful paper evidence for an exact candidate commit."""

from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

GITHUB_API = "https://api.github.com"


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fetch_json(url: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def find_fresh_paper_run(
    *,
    repository: str,
    commit: str,
    token: str,
    max_age: timedelta,
    now: datetime,
    fetcher: Callable[[str, str], dict[str, Any]] = fetch_json,
) -> dict[str, Any] | None:
    query = urllib.parse.urlencode(
        {
            "event": "workflow_dispatch",
            "status": "success",
            "head_sha": commit,
            "per_page": 100,
        }
    )
    runs_url = f"{GITHUB_API}/repos/{repository}/actions/workflows/paper.yml/runs?{query}"
    runs = fetcher(runs_url, token).get("workflow_runs", [])
    for run in sorted(runs, key=lambda item: item.get("created_at", ""), reverse=True):
        if run.get("head_sha") != commit or run.get("conclusion") != "success":
            continue
        created_at = _parse_time(run["created_at"])
        if now - created_at > max_age or created_at > now + timedelta(minutes=5):
            continue
        artifacts = fetcher(run["artifacts_url"], token).get("artifacts", [])
        expected_name = f"paper-{commit}-{run['id']}"
        if any(
            artifact.get("name") == expected_name
            and not artifact.get("expired", True)
            and _parse_time(artifact["created_at"]) >= created_at
            for artifact in artifacts
        ):
            return {
                "run_id": run["id"],
                "run_url": run.get("html_url"),
                "created_at": run["created_at"],
                "artifact": expected_name,
            }
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--max-age-days", type=int, default=7)
    parser.add_argument("--output")
    args = parser.parse_args()
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is required to read workflow evidence")
    evidence = find_fresh_paper_run(
        repository=args.repository,
        commit=args.commit,
        token=token,
        max_age=timedelta(days=args.max_age_days),
        now=datetime.now(UTC),
    )
    report = {
        "schema_version": 1,
        "repository": args.repository,
        "commit": args.commit,
        "max_age_days": args.max_age_days,
        "evidence": evidence,
        "passed": evidence is not None,
    }
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"paper evidence: {'PASS' if evidence else 'FAIL'} for commit {args.commit}")
    if evidence:
        print(f"run_id={evidence['run_id']} created_at={evidence['created_at']}")
    return int(evidence is None)


if __name__ == "__main__":
    raise SystemExit(main())
