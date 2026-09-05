"""Require exact-candidate exercises and matching retained provider evidence."""

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
validate_okx_report = _feeds.validate_okx_report
validate_okx_soak_report = _feeds.validate_soak_report
PaperQualificationError = _paper.PaperQualificationError
validate_bundle = _paper.validate_bundle
validate_provider_report = _paper.validate_provider_report
validate_provider_soak_report = _paper.validate_provider_soak_report
verify_candidate_manifest = _paper.verify_candidate_manifest
_candidate_identity = _paper._candidate_identity
_contracts = importlib.import_module("scripts.qualification.provider_contract")
provider_contract_matches = _contracts.provider_contract_matches

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
    path = urllib.parse.urlsplit(url).path
    accept = (
        "application/vnd.github+json"
        if "/actions/artifacts/" in path
        else "application/octet-stream"
    )
    request = urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    opener = urllib.request.build_opener(_ArtifactRedirectHandler())
    with opener.open(request, timeout=30) as response:
        return response.read()


def _unique_json(archive: zipfile.ZipFile, suffix: str) -> dict[str, Any]:
    matches = [name for name in archive.namelist() if name.endswith(suffix)]
    if len(matches) != 1:
        raise PaperQualificationError(f"paper artifact has no unique {suffix}")
    loaded = json.loads(archive.read(matches[0]))
    if not isinstance(loaded, dict):
        raise PaperQualificationError(f"{suffix} is not a JSON object")
    return loaded


def _optional_json(archive: zipfile.ZipFile, suffix: str) -> dict[str, Any] | None:
    matches = [name for name in archive.namelist() if name.endswith(suffix)]
    if not matches:
        return None
    if len(matches) != 1:
        raise PaperQualificationError(f"paper artifact has no unique {suffix}")
    loaded = json.loads(archive.read(matches[0]))
    if not isinstance(loaded, dict):
        raise PaperQualificationError(f"{suffix} is not a JSON object")
    return loaded


def validate_evidence_archive(payload: bytes, expected_commit: str) -> dict[str, Any]:
    """Validate exact-candidate exercises and any retained extended reports."""
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        manifest = _unique_json(archive, "candidate.json")
        verify_candidate_manifest(manifest)
        identity = _candidate_identity(manifest)
        if identity["commit"] != expected_commit:
            raise PaperQualificationError("paper artifact targets a different commit")
        for provider in ("alpaca", "ib"):
            for phase in ("exercise", "restart"):
                validate_provider_report(
                    _unique_json(archive, f"{provider}-{phase}.json"),
                    identity,
                    provider,
                    phase,
                )
        validate_okx_report(_unique_json(archive, "okx.json"), manifest)
        soaks: dict[str, dict[str, Any]] = {}
        for provider, suffix in {
            "alpaca": "alpaca-soak.json",
            "ib": "ib-soak.json",
            "okx": "okx-soak.json",
        }.items():
            report = _optional_json(archive, suffix)
            if report is None:
                continue
            if provider == "okx":
                validate_okx_soak_report(report, manifest)
            else:
                validate_provider_soak_report(report, identity, provider)
            soaks[provider] = report
    return {"candidate": identity, "soaks": soaks}


def _archive_source(
    *,
    repository: str,
    run: dict[str, Any],
    token: str,
    fetcher: Callable[[str, str], dict[str, Any]],
) -> tuple[str, str] | None:
    evidence_commit = str(run["head_sha"])
    expected_name = f"paper-{evidence_commit}-{run['id']}"
    artifacts = fetcher(run["artifacts_url"], token).get("artifacts", [])
    matching = [
        artifact
        for artifact in artifacts
        if artifact.get("name") == expected_name
        and not artifact.get("expired", True)
        and artifact.get("archive_download_url")
    ]
    if len(matching) == 1:
        return expected_name, str(matching[0]["archive_download_url"])

    release_url = f"{GITHUB_API}/repos/{repository}/releases/tags/provider-evidence-{run['id']}"
    try:
        release = fetcher(release_url, token)
    except OSError:
        return None
    assets = [
        asset
        for asset in release.get("assets", [])
        if asset.get("name") == "provider-evidence.zip" and asset.get("url")
    ]
    if len(assets) != 1:
        return None
    return f"provider-evidence-{run['id']}/provider-evidence.zip", str(assets[0]["url"])


def find_paper_evidence(
    *,
    repository: str,
    commit: str,
    token: str,
    checkout_root: Path,
    fetcher: Callable[[str, str], dict[str, Any]] = fetch_json,
    downloader: Callable[[str, str], bytes] = fetch_bytes,
    contract_matcher: Callable[..., bool] = provider_contract_matches,
) -> dict[str, Any] | None:
    query = urllib.parse.urlencode(
        {
            "event": "workflow_dispatch",
            "status": "success",
            "per_page": 100,
        }
    )
    runs_url = f"{GITHUB_API}/repos/{repository}/actions/workflows/paper.yml/runs?{query}"
    runs = fetcher(runs_url, token).get("workflow_runs", [])
    candidate_evidence: dict[str, Any] | None = None
    provider_evidence: dict[str, dict[str, Any]] = {}
    for run in sorted(runs, key=lambda item: item.get("created_at", ""), reverse=True):
        evidence_commit = run.get("head_sha")
        if (
            not isinstance(evidence_commit, str)
            or run.get("conclusion") != "success"
            or run.get("status", "completed") != "completed"
        ):
            continue
        created_at = _parse_time(run["created_at"])
        if created_at > datetime.now(UTC) + timedelta(minutes=5):
            continue
        source = _archive_source(
            repository=repository,
            run=run,
            token=token,
            fetcher=fetcher,
        )
        if source is None:
            continue
        evidence_name, download_url = source
        try:
            archive = validate_evidence_archive(downloader(download_url, token), evidence_commit)
        except (
            PaperQualificationError,
            FeedQualificationError,
            ValueError,
            KeyError,
            json.JSONDecodeError,
            zipfile.BadZipFile,
        ):
            continue
        identity = archive["candidate"]
        if evidence_commit == commit and candidate_evidence is None:
            candidate_evidence = {
                "run_id": run["id"],
                "run_url": run.get("html_url"),
                "created_at": run["created_at"],
                "artifact": evidence_name,
                "qualification_run_id": identity["qualification_run_id"],
                "wheel_sha256": identity["wheel_sha256"],
                "sdist_sha256": identity["sdist_sha256"],
            }
        for provider, report in archive["soaks"].items():
            if provider in provider_evidence:
                continue
            if contract_matcher(
                provider,
                evidence_commit=evidence_commit,
                candidate_commit=commit,
                checkout_root=checkout_root,
                reported_contract=report.get("provider_contract"),
            ):
                provider_evidence[provider] = {
                    "run_id": run["id"],
                    "run_url": run.get("html_url"),
                    "commit": evidence_commit,
                    "completed_at": report["completed_at"],
                    "artifact": evidence_name,
                }
    if candidate_evidence is None or set(provider_evidence) != {"alpaca", "ib", "okx"}:
        return None
    return {**candidate_evidence, "providers": provider_evidence}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is required to read workflow evidence")
    evidence = find_paper_evidence(
        repository=args.repository,
        commit=args.commit,
        token=token,
        checkout_root=REPOSITORY_ROOT,
    )
    report = {
        "schema_version": 1,
        "repository": args.repository,
        "commit": args.commit,
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
            output.write(f"sdist_sha256={evidence['sdist_sha256']}\n")
    print(f"paper evidence: {'PASS' if evidence else 'FAIL'} for commit {args.commit}")
    if evidence:
        print(
            f"run_id={evidence['run_id']} created_at={evidence['created_at']} "
            f"wheel_sha256={evidence['wheel_sha256']} "
            f"sdist_sha256={evidence['sdist_sha256']}"
        )
    return int(evidence is None)


if __name__ == "__main__":
    raise SystemExit(main())
