"""Repeat public-runtime reliability tests through isolated candidate wheels."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from time import monotonic

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BUILD_CONSTRAINTS = REPOSITORY_ROOT / "build-constraints.txt"
TEST_DEPENDENCIES = (
    "pytest==9.1.1",
    "pytest-asyncio==1.4.0",
    "pytest-timeout==2.4.0",
)
RELIABILITY_TESTS = (
    Path("tests/unit/test_engine_transactions.py"),
    Path("tests/unit/test_engine.py"),
    Path("tests/unit/test_secure_persistence.py"),
    Path("tests/unit/test_order_contract.py"),
    Path("tests/unit/test_strategy_runtime.py"),
    Path("tests/unit/test_broker_contract.py"),
    Path("tests/unit/test_safe_broker.py"),
    Path("tests/integration/test_execution_modes.py"),
)
PROFILES = (
    ("3.12", "1", RELIABILITY_TESTS),
    ("3.13", "3571", tuple(reversed(RELIABILITY_TESTS))),
    ("3.14", "4294967295", RELIABILITY_TESTS[3:] + RELIABILITY_TESTS[:3]),
)
EXPECTED_DISTRIBUTIONS = {
    "ml4t-backtest": "0.1.0",
    "ml4t-live": "0.1.0",
    "ml4t-specs": "0.1.1",
}
PYTEST_INI = """\
[pytest]
asyncio_mode = auto
asyncio_default_fixture_loop_scope = function
markers =
    deterministic: requires no network, credentials, or external process
    integration: exercises multiple package components
"""
VERSION_PROBE = """\
import importlib.metadata
import json

names = ("ml4t-backtest", "ml4t-live", "ml4t-specs")
print(json.dumps({name: importlib.metadata.version(name) for name in names}, sort_keys=True))
"""


def run(command: list[str], *, cwd: Path, environment: dict[str, str] | None = None) -> str:
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
            f"command failed ({result.returncode}): {' '.join(command[:5])}\n{detail}"
        )
    return result.stdout


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def passed_test_count(output: str) -> int:
    matches = re.findall(r"(?<!\d)(\d+) passed", output)
    if not matches:
        raise RuntimeError(f"pytest did not report a passing test count:\n{output}")
    return int(matches[-1])


def qualify(output: Path | None = None) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="ml4t-live-reliability-") as temporary:
        root = Path(temporary)
        dist = root / "dist"
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTEST_ADDOPTS", None)
        environment["SETUPTOOLS_SCM_PRETEND_VERSION"] = EXPECTED_DISTRIBUTIONS["ml4t-live"]
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
        for source in RELIABILITY_TESTS:
            destination = isolated_tests / source.relative_to("tests")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPOSITORY_ROOT / source, destination)
        (root / "pytest.ini").write_text(PYTEST_INI)

        profiles = []
        expected_count: int | None = None
        for version, hash_seed, test_order in PROFILES:
            venv = root / f"python-{version}"
            run(["uv", "venv", "--python", version, str(venv)], cwd=root)
            python = venv / "bin" / "python"
            run(
                ["uv", "pip", "install", "--python", str(python), str(wheel), *TEST_DEPENDENCIES],
                cwd=root,
            )
            profile_environment = environment | {"PYTHONHASHSEED": hash_seed}
            installed = json.loads(
                run(
                    [str(python), "-I", "-c", VERSION_PROBE],
                    cwd=root,
                    environment=profile_environment,
                )
            )
            if installed != EXPECTED_DISTRIBUTIONS:
                raise RuntimeError(
                    f"Python {version} resolved unexpected distributions: {installed}"
                )
            started = monotonic()
            pytest_output = run(
                [
                    str(python),
                    "-I",
                    "-m",
                    "pytest",
                    "-q",
                    "--strict-markers",
                    "--timeout=10",
                    *(str(isolated_tests / path.relative_to("tests")) for path in test_order),
                ],
                cwd=root,
                environment=profile_environment,
            )
            elapsed = monotonic() - started
            count = passed_test_count(pytest_output)
            if expected_count is not None and count != expected_count:
                raise RuntimeError(
                    f"Python {version} collected {count} tests; expected {expected_count}"
                )
            expected_count = count
            profiles.append(
                {
                    "python": version,
                    "hash_seed": hash_seed,
                    "test_order": [str(path) for path in test_order],
                    "passed": True,
                    "test_count": count,
                    "elapsed_seconds": round(elapsed, 3),
                    "distributions": installed,
                }
            )

        report: dict[str, object] = {
            "schema_version": 1,
            "source_revision": run(["git", "rev-parse", "HEAD"], cwd=REPOSITORY_ROOT).strip(),
            "wheel": wheel.name,
            "wheel_sha256": sha256(wheel),
            "profiles": profiles,
            "reliability_tests": [str(path) for path in RELIABILITY_TESTS],
            "terminal_resource_deadline_seconds": 5,
        }
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    print(json.dumps(qualify(args.output), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
