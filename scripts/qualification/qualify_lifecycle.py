"""Exercise lifecycle and order contracts through isolated candidate wheels."""

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

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BUILD_CONSTRAINTS = REPOSITORY_ROOT / "build-constraints.txt"
SUPPORTED_PYTHONS = ("3.12", "3.13", "3.14")
TEST_DEPENDENCIES = (
    "pytest==9.1.1",
    "pytest-asyncio==1.4.0",
    "pytest-timeout==2.4.0",
)
QUALIFICATION_TESTS = (
    Path("tests/contracts/test_causal_strategy_parity.py"),
    Path("tests/unit/test_broker_contract.py"),
    Path("tests/unit/test_order_contract.py"),
    Path("tests/unit/test_strategy_runtime.py"),
)
EXPECTED_DISTRIBUTIONS = {
    "ml4t-backtest": "0.1.0",
    "ml4t-live": "0.1.0",
    "ml4t-specs": "0.1.1",
}
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
    with tempfile.TemporaryDirectory(prefix="ml4t-live-lifecycle-") as temporary:
        root = Path(temporary)
        dist = root / "dist"
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
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
        for source in QUALIFICATION_TESTS:
            destination = isolated_tests / source.relative_to("tests")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPOSITORY_ROOT / source, destination)

        profiles = []
        expected_count: int | None = None
        for version in SUPPORTED_PYTHONS:
            venv = root / f"python-{version}"
            run(["uv", "venv", "--python", version, str(venv)], cwd=root)
            python = venv / "bin" / "python"
            run(
                ["uv", "pip", "install", "--python", str(python), str(wheel), *TEST_DEPENDENCIES],
                cwd=root,
            )
            installed = json.loads(
                run([str(python), "-I", "-c", VERSION_PROBE], cwd=root, environment=environment)
            )
            if installed != EXPECTED_DISTRIBUTIONS:
                raise RuntimeError(
                    f"Python {version} resolved unexpected distributions: {installed}"
                )
            pytest_output = run(
                [
                    str(python),
                    "-I",
                    "-m",
                    "pytest",
                    "-q",
                    "--strict-markers",
                    "--timeout=60",
                    str(isolated_tests),
                ],
                cwd=root,
                environment=environment,
            )
            count = passed_test_count(pytest_output)
            if expected_count is not None and count != expected_count:
                raise RuntimeError(
                    f"Python {version} collected {count} tests; expected {expected_count}"
                )
            expected_count = count
            profiles.append(
                {
                    "python": version,
                    "passed": True,
                    "test_count": count,
                    "distributions": installed,
                }
            )

        report: dict[str, object] = {
            "schema_version": 1,
            "source_revision": run(["git", "rev-parse", "HEAD"], cwd=REPOSITORY_ROOT).strip(),
            "wheel": wheel.name,
            "wheel_sha256": sha256(wheel),
            "profiles": profiles,
            "qualification_tests": [str(path) for path in QUALIFICATION_TESTS],
            "fault_detection_tests": [
                "test_contract_comparator_detects_runtime_branch_fault",
                "test_ib_order_updates_are_monotonic_and_terminal",
                "test_alpaca_order_updates_are_monotonic_across_cancel_race",
                "test_invalid_or_failed_snapshot_never_becomes_clean",
                "test_failed_cancel_and_resubmit_gap_is_persisted_and_visible",
                "test_venue_failure_policy_halts_or_retries_with_stable_idempotency",
            ],
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
