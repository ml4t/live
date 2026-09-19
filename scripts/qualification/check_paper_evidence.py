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
_release = importlib.import_module("scripts.qualification.verify_release_identity")
artifact_identity = _release.artifact_identity

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
    """Validate every complete provider result present in an evidence archive."""
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        manifest = _unique_json(archive, "candidate.json")
        verify_candidate_manifest(manifest)
        identity = _candidate_identity(manifest)
        if identity["commit"] != expected_commit:
            raise PaperQualificationError("paper artifact targets a different commit")
        providers: dict[str, dict[str, Any]] = {}
        for provider in ("alpaca", "ib"):
            reports = {
                phase: _optional_json(archive, f"{provider}-{phase}.json")
                for phase in ("exercise", "restart")
            }
            soak = _optional_json(archive, f"{provider}-soak.json")
            present = [*reports.values(), soak]
            if not any(present):
                continue
            if not all(present):
                raise PaperQualificationError(f"paper artifact has incomplete {provider} evidence")
            for phase, report in reports.items():
                if report is None:
                    raise PaperQualificationError(
                        f"paper artifact has incomplete {provider} evidence"
                    )
                validate_provider_report(report, identity, provider, phase)
            if soak is None:
                raise PaperQualificationError(f"paper artifact has incomplete {provider} evidence")
            validate_provider_soak_report(soak, identity, provider)
            providers[provider] = {**reports, "soak": soak}
        okx = _optional_json(archive, "okx.json")
        okx_soak = _optional_json(archive, "okx-soak.json")
        if okx is not None or okx_soak is not None:
            if okx is None or okx_soak is None:
                raise FeedQualificationError("paper artifact has incomplete okx evidence")
            validate_okx_report(okx, manifest)
            validate_okx_soak_report(okx_soak, manifest)
            providers["okx"] = {"exercise": okx, "soak": okx_soak}
    return {"candidate": identity, "providers": providers}


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
    candidate_artifacts: dict[str, dict[str, str]],
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
        for provider, reports in archive["providers"].items():
            if provider in provider_evidence:
                continue
            report = reports["soak"]
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
    if set(provider_evidence) != {"alpaca", "ib", "okx"}:
        return None
    return {
        "commit": commit,
        "wheel_sha256": candidate_artifacts["wheel"]["sha256"],
        "sdist_sha256": candidate_artifacts["sdist"]["sha256"],
        "providers": provider_evidence,
    }


def write_github_output(path: Path, evidence: dict[str, Any] | None) -> None:
    """Write release artifact hashes only after every provider has valid evidence."""
    if evidence is None:
        return
    with path.open("a") as output:
        output.write(f"wheel_sha256={evidence['wheel_sha256']}\n")
        output.write(f"sdist_sha256={evidence['sdist_sha256']}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--artifacts-dir", type=Path, required=True)
    parser.add_argument("--output")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is required to read workflow evidence")
    _, candidate_artifacts = artifact_identity(args.artifacts_dir)
    evidence = find_paper_evidence(
        repository=args.repository,
        commit=args.commit,
        token=token,
        checkout_root=REPOSITORY_ROOT,
        candidate_artifacts=candidate_artifacts,
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
    if args.github_output:
        write_github_output(args.github_output, evidence)
    print(f"paper evidence: {'PASS' if evidence else 'FAIL'} for commit {args.commit}")
    if evidence:
        print(f"wheel_sha256={evidence['wheel_sha256']} sdist_sha256={evidence['sdist_sha256']}")
        for provider, source in evidence["providers"].items():
            print(
                f"provider={provider} run_id={source['run_id']} "
                f"commit={source['commit']} completed_at={source['completed_at']}"
            )
    return int(evidence is None)


if __name__ == "__main__":
    raise SystemExit(main())
