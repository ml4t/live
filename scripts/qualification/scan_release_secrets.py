"""Scan release inputs for credentials without emitting matched values."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tarfile
import zipfile
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TEXT_SUFFIXES = {
    ".cfg",
    ".env",
    ".ini",
    ".json",
    ".jsonl",
    ".log",
    ".md",
    ".py",
    ".rst",
    ".sh",
    ".toml",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
SOURCE_ROOTS = ("src", "examples", "scripts", "docs", ".github")
ROOT_FILES = (
    "README.md",
    "api.yaml",
    "DESIGN.md",
    "pyproject.toml",
    "uv.lock",
    "dependency-policy.toml",
    "artifact-manifest.toml",
    "release-recovery.toml",
)
HISTORY_PATHS = (*SOURCE_ROOTS, *ROOT_FILES)
MAX_FILE_BYTES = 5_000_000

SECRET_PATTERNS = {
    "aws-access-key": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "github-token": re.compile(
        rb"\b(?:gh[opusr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{80,255})\b"
    ),
    "openai-api-key": re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    "private-key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "credential-assignment": re.compile(
        rb"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password)"
        rb"\b\s*[:=]\s*['\"][A-Za-z0-9_./+=:-]{16,}['\"]"
    ),
    "broker-account-id": re.compile(rb"\b(?:DU|DF|F|U)\d{7,10}\b"),
    "url-basic-auth": re.compile(rb"https?://[^/\s:@]+:[^@\s/]+@"),
    "signed-url-query": re.compile(
        rb"(?i)https?://[^\s]+[?&](?:x-amz-signature|signature|sig|token)=[A-Za-z0-9_%+-]{12,}"
    ),
}


@dataclass(frozen=True)
class ScanFinding:
    pattern: str
    location_digest: str
    occurrence_count: int


@dataclass(frozen=True)
class ScanResult:
    sources: int
    bytes_scanned: int
    findings: tuple[ScanFinding, ...]


def _location_digest(location: str) -> str:
    return hashlib.sha256(location.encode()).hexdigest()[:16]


def scan_payloads(payloads: Iterable[tuple[str, bytes]]) -> ScanResult:
    findings: list[ScanFinding] = []
    sources = 0
    bytes_scanned = 0
    for location, payload in payloads:
        sources += 1
        bytes_scanned += len(payload)
        for name, pattern in SECRET_PATTERNS.items():
            count = len(pattern.findall(payload))
            if count:
                findings.append(ScanFinding(name, _location_digest(location), count))
    return ScanResult(sources, bytes_scanned, tuple(findings))


def _text_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return
    candidates = (root,) if root.is_file() else root.rglob("*")
    for path in candidates:
        if (
            path.is_file()
            and path.suffix.lower() in TEXT_SUFFIXES
            and path.stat().st_size <= MAX_FILE_BYTES
            and ".git" not in path.parts
            and ".venv" not in path.parts
        ):
            yield path


def current_source_payloads(repository: Path) -> Iterable[tuple[str, bytes]]:
    for relative in SOURCE_ROOTS:
        root = repository / relative
        for path in _text_files(root):
            yield f"source:{path.relative_to(repository)}", path.read_bytes()
    for path in repository.rglob("*.log"):
        if path.is_file() and path.stat().st_size <= MAX_FILE_BYTES and ".git" not in path.parts:
            yield f"log:{path.relative_to(repository)}", path.read_bytes()
    for relative in ROOT_FILES:
        path = repository / relative
        if path.is_file() and path.stat().st_size <= MAX_FILE_BYTES:
            yield f"source:{relative}", path.read_bytes()


def history_payload(repository: Path) -> tuple[str, bytes]:
    result = subprocess.run(
        ["git", "log", "--all", "--format=commit:%H", "-p", "--", *HISTORY_PATHS],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    return "git-history:selected-release-paths", result.stdout


def artifact_payloads(paths: Iterable[Path]) -> Iterable[tuple[str, bytes]]:
    for path in paths:
        if path.suffix == ".whl":
            with zipfile.ZipFile(path) as archive:
                for member in archive.infolist():
                    if not member.is_dir() and member.file_size <= MAX_FILE_BYTES:
                        yield f"artifact:{path.name}:{member.filename}", archive.read(member)
        elif path.name.endswith(".tar.gz"):
            with tarfile.open(path) as archive:
                for member in archive.getmembers():
                    if member.isfile() and member.size <= MAX_FILE_BYTES:
                        extracted = archive.extractfile(member)
                        if extracted is not None:
                            yield f"artifact:{path.name}:{member.name}", extracted.read()
        else:
            raise ValueError(f"unsupported artifact: {path}")


def evidence_payloads(root: Path | None) -> Iterable[tuple[str, bytes]]:
    if root is None or not root.exists():
        return
    for path in _text_files(root):
        yield f"evidence:{path.relative_to(root)}", path.read_bytes()


def scan_release(
    repository: Path,
    artifacts: Iterable[Path],
    evidence_root: Path | None,
) -> ScanResult:
    payloads = (
        *current_source_payloads(repository),
        history_payload(repository),
        *artifact_payloads(artifacts),
        *evidence_payloads(evidence_root),
    )
    return scan_payloads(payloads)


def _default_evidence_root(repository: Path) -> Path | None:
    candidate = (
        repository.parent / "ml4t-live-dev" / ".workspace" / "work" / "ml4t-live-stable-readiness"
    )
    return candidate if candidate.exists() else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifacts", nargs="*", type=Path)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    evidence_root = args.evidence_root or _default_evidence_root(REPOSITORY_ROOT)
    result = scan_release(REPOSITORY_ROOT, args.artifacts, evidence_root)
    report = {
        "schema_version": 1,
        "repository": str(REPOSITORY_ROOT),
        "evidence_included": evidence_root is not None,
        "sources": result.sources,
        "bytes_scanned": result.bytes_scanned,
        "findings": [asdict(finding) for finding in result.findings],
        "passed": not result.findings,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(
        f"release secret scan: {'PASS' if report['passed'] else 'FAIL'} "
        f"({result.sources} sources, {result.bytes_scanned} bytes, "
        f"{len(result.findings)} redacted findings)"
    )
    for finding in result.findings:
        print(
            f"pattern={finding.pattern} location_digest={finding.location_digest} "
            f"occurrences={finding.occurrence_count}"
        )
    return int(bool(result.findings))


if __name__ == "__main__":
    raise SystemExit(main())
