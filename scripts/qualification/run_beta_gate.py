"""Run the complete credential-free beta qualification gate."""

from __future__ import annotations

import glob
import os
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_SCOPES = ("src", "tests", "examples", "scripts")
COVERAGE_MINIMUM = "80"

CRITICAL_FAULT_TESTS = (
    "tests/contracts/test_causal_strategy_parity.py::"
    "test_contract_comparator_detects_runtime_branch_fault",
    "tests/unit/test_broker_contract.py::"
    "test_seeded_real_adapter_pending_order_is_a_blocking_mismatch",
    "tests/unit/test_order_contract.py::"
    "test_invalid_request_is_atomic_before_adapter_or_persistence",
    "tests/unit/test_secure_persistence.py::test_state_integrity_tamper_fails_closed",
    "tests/unit/test_crypto_feed.py::"
    "test_final_revision_is_not_suppressed_by_timestamp_deduplication",
    "tests/unit/test_engine.py::test_queue_overflow_halts_before_pending_callback_and_records_gap",
    "tests/unit/test_strategy_runtime.py::"
    "test_recovery_preserves_target_pending_and_rule_state_without_duplicate_intent",
    "tests/unit/test_engine_transactions.py::"
    "test_shutdown_signal_uses_transactional_stop_without_task_leaks",
)


@dataclass(frozen=True)
class Stage:
    name: str
    command: tuple[str, ...]
    environment: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class StageResult:
    name: str
    returncode: int


def qualification_stages(temporary_directory: Path, repetitions: int = 5) -> list[Stage]:
    coverage_file = temporary_directory / "coverage"
    coverage_json = temporary_directory / "coverage.json"
    distribution_directory = temporary_directory / "dist"
    site_directory = temporary_directory / "site"
    stages = [
        Stage("ruff-format", ("uv", "run", "ruff", "format", "--check", *SOURCE_SCOPES)),
        Stage("ruff", ("uv", "run", "ruff", "check", *SOURCE_SCOPES)),
        Stage("types", ("uv", "run", "ty", "check")),
        Stage("pre-commit", ("uv", "run", "pre-commit", "run", "--all-files")),
        Stage(
            "dependency-audit",
            ("uv", "run", "python", "scripts/qualification/audit_dependencies.py"),
        ),
        Stage(
            "dependency-compatibility",
            ("uv", "run", "python", "scripts/qualification/check_dependency_matrix.py"),
        ),
        Stage(
            "deterministic-tests-and-branch-coverage",
            (
                "uv",
                "run",
                "pytest",
                "-q",
                "--strict-markers",
                "--timeout=60",
                "--cov=ml4t.live",
                "--cov-branch",
                "--cov-report=term-missing",
                f"--cov-report=json:{coverage_json}",
                f"--cov-fail-under={COVERAGE_MINIMUM}",
            ),
            {"COVERAGE_FILE": str(coverage_file)},
        ),
        Stage(
            "stress",
            (
                "uv",
                "run",
                "pytest",
                "-q",
                "--strict-markers",
                "--timeout=120",
                "--run-stress",
                "-m",
                "stress",
                "tests/stress",
            ),
        ),
    ]
    for repetition in range(repetitions):
        offset = repetition % len(CRITICAL_FAULT_TESTS)
        ordered_tests = CRITICAL_FAULT_TESTS[offset:] + CRITICAL_FAULT_TESTS[:offset]
        stages.append(
            Stage(
                f"critical-faults-{repetition + 1}",
                (
                    "uv",
                    "run",
                    "pytest",
                    "-q",
                    "--strict-markers",
                    "--timeout=60",
                    *ordered_tests,
                ),
                {"PYTHONHASHSEED": str(1009 + repetition)},
            )
        )
    stages.extend(
        (
            Stage(
                "documentation",
                (
                    "uv",
                    "run",
                    "mkdocs",
                    "build",
                    "--strict",
                    "--site-dir",
                    str(site_directory),
                ),
            ),
            Stage(
                "build",
                (
                    "uv",
                    "build",
                    "--out-dir",
                    str(distribution_directory),
                    "--build-constraints",
                    "build-constraints.txt",
                ),
            ),
            Stage(
                "distribution-metadata",
                (
                    "uv",
                    "run",
                    "twine",
                    "check",
                    "--strict",
                    str(distribution_directory / "*"),
                ),
            ),
        )
    )
    return stages


def repository_status() -> str:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def resolve_command(command: Sequence[str]) -> list[str]:
    resolved = []
    for argument in command:
        matches = sorted(glob.glob(argument)) if glob.has_magic(argument) else []
        resolved.extend(matches or [argument])
    return resolved


def run_stage(stage: Stage) -> int:
    environment = os.environ.copy()
    environment.update(stage.environment)
    command = resolve_command(stage.command)
    print(f"\n[{stage.name}] {' '.join(command)}", flush=True)
    result = subprocess.run(command, cwd=REPOSITORY_ROOT, env=environment, check=False)
    return result.returncode


def execute_stages(
    stages: Sequence[Stage],
    *,
    runner: Callable[[Stage], int] = run_stage,
    status: Callable[[], str] = repository_status,
) -> int:
    before = status()
    results = []
    for stage in stages:
        try:
            returncode = runner(stage)
        except Exception as error:
            print(f"[{stage.name}] ERROR: {error}", flush=True)
            returncode = 1
        results.append(StageResult(stage.name, returncode))

    after = status()
    mutated = before != after
    print("\nBeta qualification summary")
    for result in results:
        outcome = "PASS" if result.returncode == 0 else f"FAIL ({result.returncode})"
        print(f"{result.name}: {outcome}")
    print(f"candidate-worktree-unchanged: {'PASS' if not mutated else 'FAIL'}")
    return int(mutated or any(result.returncode for result in results))


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ml4t-live-beta-gate-") as temporary:
        return execute_stages(qualification_stages(Path(temporary)))


if __name__ == "__main__":
    raise SystemExit(main())
