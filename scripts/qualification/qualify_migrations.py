"""Exercise retained beta state through clean installed candidate wheels."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BUILD_CONSTRAINTS = REPOSITORY_ROOT / "build-constraints.txt"
MIGRATION_TEST = REPOSITORY_ROOT / "tests" / "integration" / "test_beta_state_migration.py"
MIGRATION_FIXTURES = REPOSITORY_ROOT / "tests" / "fixtures" / "migration"
SUPPORTED_PYTHONS = ("3.12", "3.13", "3.14")
TEST_DEPENDENCIES = ("pytest==9.1.1",)


def run(command: list[str], *, cwd: Path, environment: dict[str, str] | None = None) -> None:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(command[:4])}\n{detail}"
        )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def qualify(output: Path | None = None) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="ml4t-live-migration-") as temporary:
        root = Path(temporary)
        dist = root / "dist"
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment["SETUPTOOLS_SCM_PRETEND_VERSION"] = "0.1.0"
        run(
            [
                "uv",
                "build",
                "--wheel",
                "--out-dir",
                str(dist),
                "--build-constraints",
                str(BUILD_CONSTRAINTS),
            ],
            cwd=REPOSITORY_ROOT,
            environment=environment,
        )
        wheel = next(dist.glob("*.whl"))
        isolated_tests = root / "tests"
        integration_tests = isolated_tests / "integration"
        integration_tests.mkdir(parents=True)
        shutil.copy2(MIGRATION_TEST, integration_tests / MIGRATION_TEST.name)
        shutil.copytree(MIGRATION_FIXTURES, isolated_tests / "fixtures" / "migration")

        profiles = []
        for version in SUPPORTED_PYTHONS:
            venv = root / f"python-{version}"
            run(["uv", "venv", "--python", version, str(venv)], cwd=root)
            python = venv / "bin" / "python"
            run(
                ["uv", "pip", "install", "--python", str(python), str(wheel), *TEST_DEPENDENCIES],
                cwd=root,
            )
            run(
                [str(python), "-I", "-m", "pytest", "-q", str(isolated_tests)],
                cwd=root,
                environment=environment,
            )
            profiles.append({"python": version, "passed": True})

        report: dict[str, object] = {
            "schema_version": 1,
            "source_revision": subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=REPOSITORY_ROOT,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip(),
            "wheel": wheel.name,
            "wheel_sha256": sha256(wheel),
            "profiles": profiles,
            "fixture_provenance": json.loads((MIGRATION_FIXTURES / "provenance.json").read_text()),
        }
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = qualify(args.output)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
