"""Require fresh successful paper evidence for an exact candidate commit."""

from __future__ import annotations

import argparse
import importlib
import io
import json
import os
import sys
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from http.client import HTTPMessage
from pathlib import Path
from typing import IO, Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

_feeds = importlib.import_module("scripts.qualification.qualify_feeds")
_paper = importlib.import_module("scripts.qualification.qualify_paper")
FeedQualificationError = _feeds.FeedQualificationError
validate_feed_bundle = _feeds.validate_feed_bundle
PaperQualificationError = _paper.PaperQualificationError
validate_bundle = _paper.validate_bundle

GITHUB_API = "https://api.github.com"


class _ArtifactRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and (
            urllib.parse.urlsplit(req.full_url).netloc != urllib.parse.urlsplit(newurl).netloc
        ):
            for header in ("Authorization", "X-GitHub-Api-Version", "Accept"):
                redirected.remove_header(header)
        return redirected


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


def fetch_bytes(url: str, token: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    opener = urllib.request.build_opener(_ArtifactRedirectHandler())
    with opener.open(request, timeout=30) as response:
        return response.read()


def validate_evidence_archive(payload: bytes, expected_commit: str) -> dict[str, Any]:
    """Validate the retained bundle rather than trusting workflow metadata alone."""
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        paper_matches = [
            name for name in archive.namelist() if name.endswith("paper-qualification.json")
        ]
        feed_matches = [
            name for name in archive.namelist() if name.endswith("feed-qualification.json")
        ]
        if len(paper_matches) != 1:
            raise PaperQualificationError("paper artifact has no unique qualification bundle")
        if len(feed_matches) != 1:
            raise FeedQualificationError("paper artifact has no unique feed qualification bundle")
        loaded = json.loads(archive.read(paper_matches[0]))
        feed_loaded = json.loads(archive.read(feed_matches[0]))
    if not isinstance(loaded, dict):
        raise PaperQualificationError("paper qualification bundle is not a JSON object")
    if not isinstance(feed_loaded, dict):
        raise FeedQualificationError("feed qualification bundle is not a JSON object")
    validate_bundle(loaded, expected_commit=expected_commit)
    validate_feed_bundle(feed_loaded, expected_commit=expected_commit)
    if feed_loaded["candidate"] != loaded["candidate"]:
        raise FeedQualificationError("paper and feed bundles target different candidates")
    return loaded


def find_fresh_paper_run(
    *,
    repository: str,
    commit: str,
    token: str,
    max_age: timedelta,
    now: datetime,
    fetcher: Callable[[str, str], dict[str, Any]] = fetch_json,
    downloader: Callable[[str, str], bytes] = fetch_bytes,
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
        matching = [
            artifact
            for artifact in artifacts
            if artifact.get("name") == expected_name
            and not artifact.get("expired", True)
            and _parse_time(artifact["created_at"]) >= created_at
            and artifact.get("archive_download_url")
        ]
        if len(matching) != 1:
            continue
        try:
            bundle = validate_evidence_archive(
                downloader(str(matching[0]["archive_download_url"]), token), commit
            )
        except (
            PaperQualificationError,
            FeedQualificationError,
            ValueError,
            KeyError,
            json.JSONDecodeError,
            zipfile.BadZipFile,
        ):
            continue
        bundle_created_at = _parse_time(bundle["generated_at"])
        if bundle_created_at < created_at or bundle_created_at > now + timedelta(minutes=5):
            continue
        if bundle["passed"]:
            return {
                "run_id": run["id"],
                "run_url": run.get("html_url"),
                "created_at": run["created_at"],
                "artifact": expected_name,
                "qualification_run_id": bundle["candidate"]["qualification_run_id"],
                "wheel_sha256": bundle["candidate"]["wheel_sha256"],
            }
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--max-age-days", type=int, default=7)
    parser.add_argument("--output")
    parser.add_argument("--github-output", type=Path)
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
    if args.github_output and evidence:
        with args.github_output.open("a") as output:
            output.write(f"wheel_sha256={evidence['wheel_sha256']}\n")
    print(f"paper evidence: {'PASS' if evidence else 'FAIL'} for commit {args.commit}")
    if evidence:
        print(
            f"run_id={evidence['run_id']} created_at={evidence['created_at']} "
            f"wheel_sha256={evidence['wheel_sha256']}"
        )
    return int(evidence is None)


if __name__ == "__main__":
    raise SystemExit(main())
