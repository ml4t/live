"""Tests for the authoritative beta qualification command."""

from __future__ import annotations

import runpy
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

SCRIPT = Path(__file__).parents[2] / "scripts" / "qualification" / "run_beta_gate.py"
NAMESPACE = runpy.run_path(str(SCRIPT))
Stage = NAMESPACE["Stage"]


def test_gate_continues_after_a_seeded_stage_failure() -> None:
    execute_stages = cast(Callable[..., int], NAMESPACE["execute_stages"])
    stages = [Stage("seeded-failure", ("false",)), Stage("must-still-run", ("true",))]
    calls = []

    def runner(stage: Any) -> int:
        calls.append(stage.name)
        return int(stage.name == "seeded-failure")

    assert execute_stages(stages, runner=runner, status=lambda: "clean") == 1
    assert calls == ["seeded-failure", "must-still-run"]


def test_gate_fails_if_a_stage_mutates_the_candidate() -> None:
    execute_stages = cast(Callable[..., int], NAMESPACE["execute_stages"])
    statuses = iter(("before", "after"))

    assert (
        execute_stages(
            [Stage("passing-stage", ("true",))],
            runner=lambda stage: 0,
            status=lambda: next(statuses),
        )
        == 1
    )


def test_gate_has_explicit_topology_and_rotates_critical_fault_order(tmp_path: Path) -> None:
    qualification_stages = cast(
        Callable[[Path, int], Sequence[Any]], NAMESPACE["qualification_stages"]
    )

    stages = qualification_stages(tmp_path, 5)
    names = [stage.name for stage in stages]
    critical = [stage for stage in stages if stage.name.startswith("critical-faults-")]

    assert names[:11] == [
        "ruff-format",
        "ruff",
        "types",
        "pre-commit",
        "workflow-policy",
        "dependency-audit",
        "dependency-compatibility",
        "artifact-qualification",
        "deterministic-tests-and-branch-coverage",
        "stress",
        "performance",
    ]
    assert names[-3:] == ["documentation", "build", "distribution-metadata"]
    assert len(critical) == 5
    assert len({stage.command[6] for stage in critical}) == 5
    by_name = {stage.name: stage for stage in stages}
    assert by_name["dependency-compatibility"].environment == {
        "SETUPTOOLS_SCM_PRETEND_VERSION": "0.1.0b4"
    }
    assert by_name["artifact-qualification"].environment == {
        "SETUPTOOLS_SCM_PRETEND_VERSION": "0.1.0b4"
    }
    assert by_name["build"].environment == {"SETUPTOOLS_SCM_PRETEND_VERSION": "0.1.0b4"}


def test_gate_groups_partition_every_stage(tmp_path: Path) -> None:
    qualification_stages = cast(
        Callable[[Path, int], Sequence[Any]], NAMESPACE["qualification_stages"]
    )
    select_stage_groups = cast(Callable[..., Sequence[Any]], NAMESPACE["select_stage_groups"])
    stage_groups = cast(dict[str, frozenset[str]], NAMESPACE["STAGE_GROUPS"])
    stages = qualification_stages(tmp_path, 2)

    selected = select_stage_groups(stages, list(stage_groups))

    assert [stage.name for stage in selected] == [stage.name for stage in stages]
    assert [stage.name for stage in select_stage_groups(stages, ["stress"])] == [
        "stress",
        "critical-faults-1",
        "critical-faults-2",
    ]
