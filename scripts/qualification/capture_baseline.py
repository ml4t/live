"""Capture or verify a credential-safe qualification baseline."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SENSITIVE_ENVIRONMENT_NAMES = (
    "ALPACA_API_KEY",
    "ALPACA_PAPER",
    "ALPACA_SECRET_KEY",
    "COINBASE_API_KEY",
    "DATABENTO_API_KEY",
    "IB_ACCOUNT",
    "IB_CLIENT_ID",
    "IB_HOST",
    "IB_PORT",
    "ML4T_IB_HOST",
    "ML4T_IB_PORT",
)


def run(command: list[str], cwd: Path | None = None, *, allow_failure: bool = False) -> str:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode and not allow_failure:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(command)}: {detail}")
    return result.stdout.strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def redact_remote(remote: str) -> str:
    return re.sub(r"(https?://)[^/@]+@", r"\1[redacted]@", remote)


def run_git(path: Path, *arguments: str, allow_failure: bool = False) -> str:
    git_directory = path / ".git"
    if git_directory.is_dir():
        command = [
            "git",
            f"--git-dir={git_directory.resolve()}",
            f"--work-tree={path.resolve()}",
            *arguments,
        ]
        return run(command, allow_failure=allow_failure)
    return run(["git", *arguments], path, allow_failure=allow_failure)


def repository_record(name: str, path: Path) -> dict[str, Any]:
    if not run_git(path, "rev-parse", "--git-dir", allow_failure=True):
        raise ValueError(f"Repository {name!r} has no .git directory: {path}")
    upstream = run_git(
        path,
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        "@{upstream}",
        allow_failure=True,
    )
    remote_default = run_git(
        path,
        "symbolic-ref",
        "--short",
        "refs/remotes/origin/HEAD",
        allow_failure=True,
    )
    if not remote_default and run_git(
        path,
        "show-ref",
        "--verify",
        "refs/remotes/origin/main",
        allow_failure=True,
    ):
        remote_default = "origin/main"
    comparison_ref = upstream or remote_default
    divergence = None
    if comparison_ref:
        divergence = run_git(
            path, "rev-list", "--left-right", "--count", f"HEAD...{comparison_ref}"
        )
    remotes = {
        remote: redact_remote(run_git(path, "remote", "get-url", remote))
        for remote in run_git(path, "remote").splitlines()
    }
    tracked_inputs = {}
    for filename in ("pyproject.toml", "uv.lock", ".pre-commit-config.yaml"):
        candidate = path / filename
        if candidate.is_file():
            tracked_inputs[filename] = sha256(candidate)
    return {
        "name": name,
        "path": str(path.resolve()),
        "branch": run_git(path, "branch", "--show-current"),
        "head": run_git(path, "rev-parse", "HEAD"),
        "describe": run_git(path, "describe", "--tags", "--always", "--dirty"),
        "tags": run_git(path, "tag", "--list").splitlines(),
        "head_tags": run_git(path, "tag", "--points-at", "HEAD").splitlines(),
        "status": run_git(path, "status", "--porcelain=v1").splitlines(),
        "remotes": remotes,
        "upstream": upstream or None,
        "comparison_ref": comparison_ref or None,
        "ahead_behind": divergence,
        "input_sha256": tracked_inputs,
    }


def host_memory_bytes() -> int | None:
    meminfo = Path("/proc/meminfo")
    if not meminfo.exists():
        return None
    match = re.search(r"^MemTotal:\s+(\d+)\s+kB$", meminfo.read_text(), re.MULTILINE)
    return int(match.group(1)) * 1024 if match else None


def tool_record(live_path: Path) -> dict[str, str]:
    commands = {
        "git": ["git", "--version"],
        "gh": ["gh", "--version"],
        "uv": ["uv", "--version"],
        "pre_commit": ["pre-commit", "--version"],
        "ruff": ["uv", "run", "--no-sync", "ruff", "--version"],
        "ty": ["uv", "run", "--no-sync", "ty", "--version"],
        "pytest": ["uv", "run", "--no-sync", "pytest", "--version"],
    }
    return {name: run(command, live_path).splitlines()[0] for name, command in commands.items()}


def capture(repositories: dict[str, Path], inputs: list[Path]) -> dict[str, Any]:
    live_path = repositories["live"]
    return {
        "schema_version": 1,
        "captured_at": datetime.now(UTC).isoformat(),
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
            "memory_bytes": host_memory_bytes(),
        },
        "tools": tool_record(live_path),
        "installed_pythons": run(["uv", "python", "list", "--only-installed"]).splitlines(),
        "repositories": {
            name: repository_record(name, path) for name, path in sorted(repositories.items())
        },
        "qualification_inputs": {
            str(path.resolve()): sha256(path) for path in sorted(inputs) if path.is_file()
        },
        "external_configuration_present": {
            name: bool(os.environ.get(name)) for name in SENSITIVE_ENVIRONMENT_NAMES
        },
    }


def stable_identity(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "host": record["host"],
        "tools": record["tools"],
        "installed_pythons": record["installed_pythons"],
        "repositories": record["repositories"],
        "qualification_inputs": record["qualification_inputs"],
        "external_configuration_present": record["external_configuration_present"],
    }


def parse_repository(value: str) -> tuple[str, Path]:
    name, separator, raw_path = value.partition("=")
    if not separator or not name or not raw_path:
        raise argparse.ArgumentTypeError("Repository must use NAME=PATH")
    return name, Path(raw_path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", action="append", type=parse_repository, required=True)
    parser.add_argument("--input", action="append", type=Path, default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    repositories = dict(args.repository)
    if "live" not in repositories:
        parser.error("One --repository must be named live")
    if bool(args.output) == bool(args.verify):
        parser.error("Choose exactly one of --output or --verify")

    current = capture(repositories, args.input)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        os.replace(temporary, args.output)
        print(args.output)
        return 0

    expected = json.loads(args.verify.read_text())
    if stable_identity(expected) == stable_identity(current):
        print("baseline verified")
        return 0
    expected_identity = stable_identity(expected)
    current_identity = stable_identity(current)
    drift = [
        key for key in expected_identity if expected_identity.get(key) != current_identity.get(key)
    ]
    print(json.dumps({"baseline_valid": False, "drift_sections": drift}, indent=2))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
