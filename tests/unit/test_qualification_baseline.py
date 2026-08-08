"""Tests for beta qualification baseline capture."""

from __future__ import annotations

import argparse
import runpy
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "qualification" / "capture_baseline.py"
NAMESPACE = runpy.run_path(str(SCRIPT))


def git(path: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def test_redact_remote_removes_embedded_credentials() -> None:
    redact_remote = cast(Callable[[str], str], NAMESPACE["redact_remote"])

    assert (
        redact_remote("https://person:secret@example.test/owner/repo.git")
        == "https://[redacted]@example.test/owner/repo.git"
    )
    assert redact_remote("git@example.test:owner/repo.git") == "git@example.test:owner/repo.git"


def test_parse_repository_requires_name_and_path() -> None:
    parse_repository = cast(Callable[[str], tuple[str, Path]], NAMESPACE["parse_repository"])

    assert parse_repository("live=/tmp/live") == ("live", Path("/tmp/live"))
    with pytest.raises(argparse.ArgumentTypeError):
        parse_repository("/tmp/live")


def test_repository_record_is_clean_remote_comparable_and_redacted(tmp_path: Path) -> None:
    repository_record = cast(Callable[[str, Path], dict[str, Any]], NAMESPACE["repository_record"])
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.name", "Qualification Test")
    git(tmp_path, "config", "user.email", "qualification@example.test")
    tracked = tmp_path / "pyproject.toml"
    tracked.write_text("[project]\nname = 'fixture'\n")
    git(tmp_path, "add", "pyproject.toml")
    git(tmp_path, "commit", "-m", "fixture")
    git(tmp_path, "tag", "v0")
    git(tmp_path, "remote", "add", "origin", "https://person:secret@example.test/repo.git")
    git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")

    record = repository_record("fixture", tmp_path)

    assert record["status"] == []
    assert record["head_tags"] == ["v0"]
    assert record["comparison_ref"] == "origin/main"
    assert record["ahead_behind"] == "0\t0"
    assert record["remotes"] == {"origin": "https://[redacted]@example.test/repo.git"}
    assert record["input_sha256"]["pyproject.toml"]


def test_stable_identity_ignores_capture_timestamp() -> None:
    stable_identity = cast(Callable[[dict[str, Any]], dict[str, Any]], NAMESPACE["stable_identity"])
    identity = {
        "host": {},
        "tools": {},
        "installed_pythons": [],
        "repositories": {},
        "qualification_inputs": {},
        "external_configuration_present": {},
    }

    assert stable_identity({"captured_at": "first", **identity}) == stable_identity(
        {"captured_at": "second", **identity}
    )
