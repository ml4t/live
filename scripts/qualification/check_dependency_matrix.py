"""Test the installed candidate against every dependency resolution profile."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from packaging.markers import Marker

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
POLICY_FILE = REPOSITORY_ROOT / "dependency-policy.toml"
LOCK_FILE = REPOSITORY_ROOT / "uv.lock"
BUILD_CONSTRAINTS = REPOSITORY_ROOT / "build-constraints.txt"
SNAPSHOT_SCRIPT = REPOSITORY_ROOT / "scripts" / "qualification" / "dependency_snapshot.py"
CONTRACT_TESTS = (
    "tests/contracts",
    "tests/unit/test_broker_contract.py",
    "tests/unit/test_order_contract.py",
    "tests/unit/test_protocols.py",
    "tests/integration/test_backtest_to_live.py",
)
TEST_TOOLS = (
    "pytest==9.1.1",
    "pytest-asyncio==1.4.0",
    "pytest-timeout==2.4.0",
)


def canonical_name(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def runtime_lock_overrides(lock: dict[str, Any]) -> dict[str, str]:
    packages = {canonical_name(package["name"]): package for package in lock["package"]}
    project = packages["ml4t-live"]
    pending = [canonical_name(dependency["name"]) for dependency in project["dependencies"]]
    overrides = {}
    while pending:
        name = pending.pop()
        if name in overrides:
            continue
        package = packages[name]
        source = package.get("source", {})
        if "registry" in source:
            overrides[name] = f"{name}=={package['version']}"
        elif "git" in source:
            git_url = source["git"].split("#", 1)[0].replace("?rev=", "@")
            overrides[name] = f"{name} @ git+{git_url.removeprefix('git+')}"
        else:
            raise ValueError(f"Unsupported locked source for {name}: {source}")
        pending.extend(
            canonical_name(item["name"])
            for item in package.get("dependencies", [])
            if not item.get("marker") or Marker(item["marker"]).evaluate()
        )
    return overrides


def profile_requirements(policy: dict[str, Any], lock: dict[str, Any], profile: str) -> list[str]:
    if profile == "locked":
        return sorted(runtime_lock_overrides(lock).values())
    return sorted(
        record[profile] for record in policy["dependencies"].values() if record.get("matrix", True)
    )


def run(
    command: list[str], *, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    print(f"$ {' '.join(command)}", flush=True)
    return subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def profile_environment(
    profile: str,
    root: Path,
    wheel: Path,
    requirements: list[str],
) -> tuple[dict[str, Any] | None, list[str]]:
    failures = []
    environment_path = root / profile
    overrides_path = root / f"{profile}-overrides.txt"
    overrides_path.write_text("\n".join(requirements) + "\n")
    create = run(["uv", "venv", "--python", "3.12", str(environment_path)])
    if create.returncode:
        return None, [f"{profile} environment creation failed: {create.stderr.strip()}"]

    python = environment_path / "bin" / "python"
    install = run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--overrides",
            str(overrides_path),
            str(wheel),
            *TEST_TOOLS,
        ]
    )
    if install.returncode:
        detail = install.stderr.strip() or install.stdout.strip()
        return None, [f"{profile} installation failed: {detail}"]

    clean_environment = os.environ.copy()
    clean_environment.pop("PYTHONPATH", None)
    snapshot_result = run([str(python), str(SNAPSHOT_SCRIPT)], environment=clean_environment)
    if snapshot_result.returncode:
        failures.append(
            f"{profile} API snapshot failed: "
            f"{snapshot_result.stderr.strip() or snapshot_result.stdout.strip()}"
        )
        snapshot = None
    else:
        snapshot = json.loads(snapshot_result.stdout)

    tests = run(
        [
            str(python),
            "-m",
            "pytest",
            "-q",
            "--strict-markers",
            "--timeout=60",
            *CONTRACT_TESTS,
        ],
        environment=clean_environment,
    )
    if tests.returncode:
        failures.append(
            f"{profile} contract tests failed: {tests.stderr.strip() or tests.stdout.strip()}"
        )
    else:
        print(tests.stdout.strip())
    return snapshot, failures


def check_matrix(output: Path | None = None) -> tuple[dict[str, Any], list[str]]:
    policy = tomllib.loads(POLICY_FILE.read_text())
    lock = tomllib.loads(LOCK_FILE.read_text())
    failures = []
    snapshots = {}
    with tempfile.TemporaryDirectory(prefix="ml4t-live-dependency-matrix-") as temporary:
        root = Path(temporary)
        distribution = root / "dist"
        build = run(
            [
                "uv",
                "build",
                "--wheel",
                "--out-dir",
                str(distribution),
                "--build-constraints",
                str(BUILD_CONSTRAINTS),
            ]
        )
        if build.returncode:
            failures.append(
                f"candidate build failed: {build.stderr.strip() or build.stdout.strip()}"
            )
        else:
            wheel = next(distribution.glob("*.whl"))
            for profile in ("minimum", "locked", "maximum"):
                requirements = profile_requirements(policy, lock, profile)
                snapshot, profile_failures = profile_environment(profile, root, wheel, requirements)
                failures.extend(profile_failures)
                if snapshot is not None:
                    snapshots[profile] = snapshot

    portable_snapshots = {
        profile: snapshot["portable_api"] for profile, snapshot in snapshots.items()
    }
    if len(portable_snapshots) != 3:
        failures.append("one or more dependency profiles produced no API snapshot")
    elif len({json.dumps(value, sort_keys=True) for value in portable_snapshots.values()}) != 1:
        failures.append("portable API snapshots differ across dependency profiles")
    report = {
        "schema_version": 1,
        "profiles": {
            profile: {
                "requirements": profile_requirements(policy, lock, profile),
                "versions": snapshots.get(profile, {}).get("versions"),
            }
            for profile in ("minimum", "locked", "maximum")
        },
        "portable_api_identical": len(portable_snapshots) == 3
        and len({json.dumps(value, sort_keys=True) for value in portable_snapshots.values()}) == 1,
        "failures": failures,
    }
    if output:
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report, failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report, failures = check_matrix(args.output)
    for profile, record in report["profiles"].items():
        print(f"{profile}: {record['versions']}")
    print(f"portable-api-identical: {report['portable_api_identical']}")
    for failure in failures:
        print(f"FAIL: {failure}")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
