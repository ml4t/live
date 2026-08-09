import json
import zipfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from ml4t.backtest.types import OrderSide, OrderType
from ml4t.specs import ExecutionCapability

from ml4t.live import CanonicalOrderRequest
from scripts.qualification.qualify_paper import (
    EXERCISE_STEPS,
    RESTART_STEPS,
    PaperQualificationError,
    _assert_atomic_rejections,
    _cleanup_tags,
    _raw_tagged_orders,
    assemble_bundle,
    build_candidate_manifest,
    validate_bundle,
)
from scripts.qualification.qualify_paper import _snapshot as capture_snapshot

COMMIT = "a" * 40
WHEEL_HASH = "b" * 64


def _candidate() -> dict:
    return {
        "schema_version": 1,
        "repository": "ml4t/live",
        "commit": COMMIT,
        "qualification_run_id": 42,
        "version": "0.1.0",
        "wheel": {"filename": "ml4t_live-0.1.0-py3-none-any.whl", "sha256": WHEEL_HASH},
        "sdist": {"filename": "ml4t_live-0.1.0.tar.gz", "sha256": "c" * 64},
        "passed": True,
    }


def _snapshot() -> dict:
    return {
        "positions_count": 2,
        "pending_orders_count": 1,
        "filtered_pending_orders_count": 1,
        "position_snapshot_exact": True,
        "pending_order_snapshot_exact": True,
        "account_value_valid": True,
        "cash_valid": True,
    }


def _report(provider: str, phase: str) -> dict:
    identity = {
        "commit": COMMIT,
        "qualification_run_id": 42,
        "version": "0.1.0",
        "wheel_sha256": WHEEL_HASH,
        "sdist_sha256": "c" * 64,
    }
    snapshots = (
        {"initial": _snapshot(), "reconnect": _snapshot(), "final": _snapshot()}
        if phase == "exercise"
        else {"restart": _snapshot()}
    )
    return {
        "schema_version": 1,
        "provider": provider,
        "phase": phase,
        "candidate": identity,
        "started_at": "2026-08-08T12:00:00+00:00",
        "completed_at": "2026-08-08T12:01:00+00:00",
        "paper_identity_verified_before_submission": True,
        "steps_passed": sorted(EXERCISE_STEPS if phase == "exercise" else RESTART_STEPS),
        "snapshots": snapshots,
        "cleanup_passed": True,
        "failed_stage": None,
        "passed": True,
    }


def _reports() -> list[dict]:
    return [
        _report(provider, phase)
        for provider in ("alpaca", "ib")
        for phase in ("exercise", "restart")
    ]


def _write_wheel(path: Path, version: str = "0.1.0") -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"ml4t_live-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.4\nName: ml4t-live\nVersion: {version}\n",
        )


def test_ib_validation_warning_remains_a_tagged_working_order() -> None:
    trade = MagicMock()
    trade.order.orderRef = "ml4tq-12345678-a"
    trade.orderStatus.status = "ValidationError"
    broker = MagicMock()
    broker.ib.openTrades.return_value = [trade]

    matches = _raw_tagged_orders("ib", broker, {"ml4tq-12345678-a"})

    assert matches == [trade]


def test_candidate_manifest_binds_successful_run_and_artifact_hashes(tmp_path: Path) -> None:
    wheel = tmp_path / "ml4t_live-0.1.0-py3-none-any.whl"
    sdist = tmp_path / "ml4t_live-0.1.0.tar.gz"
    _write_wheel(wheel)
    sdist.write_bytes(b"fixed sdist")

    manifest = build_candidate_manifest(
        artifacts_directory=tmp_path,
        candidate_sha=COMMIT,
        qualification_run_id=42,
        repository="ml4t/live",
        run_record={
            "id": 42,
            "name": "CI",
            "head_sha": COMMIT,
            "status": "completed",
            "conclusion": "success",
        },
    )

    assert manifest["commit"] == COMMIT
    assert manifest["version"] == "0.1.0"
    assert len(manifest["wheel"]["sha256"]) == 64
    assert len(manifest["sdist"]["sha256"]) == 64


@pytest.mark.parametrize(
    "run_record",
    [
        {
            "id": 42,
            "name": "CI",
            "head_sha": "d" * 40,
            "status": "completed",
            "conclusion": "success",
        },
        {
            "id": 42,
            "name": "CI",
            "head_sha": COMMIT,
            "status": "completed",
            "conclusion": "failure",
        },
        {
            "id": 42,
            "name": "CI",
            "head_sha": COMMIT,
            "status": "in_progress",
            "conclusion": None,
        },
        {
            "id": 42,
            "name": "unrelated",
            "head_sha": COMMIT,
            "status": "completed",
            "conclusion": "success",
        },
    ],
)
def test_candidate_manifest_rejects_wrong_or_incomplete_run(
    tmp_path: Path, run_record: dict
) -> None:
    _write_wheel(tmp_path / "candidate.whl")
    (tmp_path / "candidate.tar.gz").write_bytes(b"sdist")

    with pytest.raises(PaperQualificationError):
        build_candidate_manifest(
            artifacts_directory=tmp_path,
            candidate_sha=COMMIT,
            qualification_run_id=42,
            repository="ml4t/live",
            run_record=run_record,
        )


def test_complete_bundle_requires_both_phases_for_both_providers() -> None:
    bundle = assemble_bundle(_candidate(), _reports(), generated_at="2026-08-08T12:02:00+00:00")

    validate_bundle(bundle, expected_commit=COMMIT)
    assert bundle["candidate"]["wheel_sha256"] == WHEEL_HASH
    assert bundle["passed"] is True


def test_bundle_rejects_report_for_different_wheel() -> None:
    reports = _reports()
    reports[0]["candidate"]["wheel_sha256"] = "d" * 64

    with pytest.raises(PaperQualificationError, match="different candidate"):
        assemble_bundle(_candidate(), reports)


def test_bundle_rejects_missing_operation_or_cleanup() -> None:
    reports = _reports()
    reports[0]["steps_passed"].remove("replace")
    reports[0]["cleanup_passed"] = False

    with pytest.raises(PaperQualificationError):
        assemble_bundle(_candidate(), reports)


def test_bundle_rejects_invalid_snapshot_instead_of_reporting_clean() -> None:
    reports = _reports()
    reports[-1]["snapshots"]["restart"]["pending_order_snapshot_exact"] = False

    with pytest.raises(PaperQualificationError, match="invalid snapshot"):
        assemble_bundle(_candidate(), reports)


def test_retained_bundle_schema_has_no_account_or_order_identifiers() -> None:
    bundle = assemble_bundle(_candidate(), deepcopy(_reports()))
    encoded = json.dumps(bundle, sort_keys=True).lower()

    assert "account_id" not in encoded
    assert "account_number" not in encoded
    assert "order_id" not in encoded
    assert "client_order_id" not in encoded
    assert "order_ref" not in encoded


def test_bundle_rejects_identifier_field_hidden_in_snapshot() -> None:
    reports = _reports()
    reports[0]["snapshots"]["initial"]["account_id"] = "redacted-looking-value"

    with pytest.raises(PaperQualificationError, match="snapshot schema"):
        assemble_bundle(_candidate(), reports)


@pytest.mark.asyncio
async def test_capability_and_policy_rejections_never_reach_provider(tmp_path: Path) -> None:
    class FakeIB:
        def __init__(self) -> None:
            self.place_calls = 0

        def placeOrder(self, *args, **kwargs):
            self.place_calls += 1
            raise AssertionError("provider submission must not run")

        def positions(self) -> list:
            return []

        def openTrades(self) -> list:
            return []

    class FakeBroker:
        execution_capabilities = frozenset({ExecutionCapability.LIMIT})

        def __init__(self) -> None:
            self.ib = FakeIB()
            self.positions = {}
            self.pending_orders = []

        def assert_paper_trading(self) -> None:
            """Identify this deterministic adapter as a paper venue."""

        async def submit_order_async(
            self,
            asset: str,
            quantity: float,
            side: OrderSide | None = None,
            order_type: OrderType = OrderType.MARKET,
            limit_price: float | None = None,
            stop_price: float | None = None,
            **kwargs,
        ):
            CanonicalOrderRequest.from_input(
                asset,
                quantity,
                side,
                order_type,
                limit_price,
                stop_price,
                capabilities=self.execution_capabilities,
            )
            return self.ib.placeOrder()

    broker = FakeBroker()

    await _assert_atomic_rejections("ib", broker, tmp_path)

    assert broker.ib.place_calls == 0


@pytest.mark.asyncio
async def test_cleanup_never_uses_order_api_when_paper_identity_is_uncertain() -> None:
    class AmbiguousBroker:
        is_connected = True

        def __init__(self) -> None:
            self.cancel_calls = 0

        def assert_paper_trading(self) -> None:
            raise RuntimeError("not verified")

        async def cancel_order_async(self, order_id: str) -> bool:
            self.cancel_calls += 1
            return True

    broker = AmbiguousBroker()

    assert await _cleanup_tags("ib", broker, {"ml4tq-12345678-a"}) is False
    assert broker.cancel_calls == 0


@pytest.mark.asyncio
async def test_alpaca_snapshot_accepts_negative_margin_cash() -> None:
    broker = MagicMock()
    broker.positions = {}
    broker.pending_orders = []
    broker._sync_positions = AsyncMock()
    broker._sync_orders = AsyncMock()
    broker._trading_client.get_all_positions.return_value = []
    broker._trading_client.get_orders.return_value = []
    broker.get_pending_orders_async = AsyncMock(return_value=[])
    broker.get_account_value_async = AsyncMock(return_value=100_000.0)
    broker.get_cash_async = AsyncMock(return_value=-1_250.50)

    snapshot = await capture_snapshot("alpaca", broker)

    assert snapshot["account_value_valid"] is True
    assert snapshot["cash_valid"] is True
    broker._sync_positions.assert_awaited_once()
    broker._sync_orders.assert_awaited_once()
