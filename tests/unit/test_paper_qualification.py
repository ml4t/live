import json
import zipfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from ml4t.backtest.types import OrderSide, OrderStatus, OrderType
from ml4t.specs import ExecutionCapability

from ml4t.live import CanonicalOrderRequest
from scripts.qualification import qualify_paper as paper_qualification
from scripts.qualification.qualify_paper import (
    EXERCISE_STEPS,
    RESTART_STEPS,
    SOAK_DURATION_SECONDS,
    SOAK_RSS_GROWTH_LIMIT_BYTES,
    SOAK_SNAPSHOT_INTERVAL_SECONDS,
    PaperQualificationError,
    _assert_atomic_rejections,
    _cleanup_tags,
    _raw_tagged_orders,
    _tag_keyword,
    assemble_bundle,
    build_candidate_manifest,
    run_provider_phase,
    run_provider_soak,
    validate_bundle,
    validate_provider_soak_report,
)
from scripts.qualification.qualify_paper import _snapshot as capture_snapshot

COMMIT = "a" * 40
WHEEL_HASH = "b" * 64


def test_paper_qualification_reads_process_rss_portably() -> None:
    assert paper_qualification._rss_bytes() > 0


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


def _soak_report(provider: str) -> dict:
    identity = {
        "commit": COMMIT,
        "qualification_run_id": 42,
        "version": "0.1.0",
        "wheel_sha256": WHEEL_HASH,
        "sdist_sha256": "c" * 64,
    }
    snapshots = [
        {
            "elapsed_seconds": index * SOAK_SNAPSHOT_INTERVAL_SECONDS,
            "rss_bytes": 100_000_000,
            "positions_count": 2,
            "pending_orders_count": 0,
            "filtered_pending_orders_count": 0,
            "position_snapshot_exact": True,
            "pending_order_snapshot_exact": True,
            "account_value_valid": True,
            "cash_valid": True,
            "connected": True,
        }
        for index in range(SOAK_DURATION_SECONDS // SOAK_SNAPSHOT_INTERVAL_SECONDS + 1)
    ]
    return {
        "schema_version": 1,
        "provider": provider,
        "candidate": identity,
        "started_at": "2026-08-08T12:00:00+00:00",
        "completed_at": "2026-08-08T18:00:01+00:00",
        "duration_seconds": SOAK_DURATION_SECONDS + 0.1,
        "snapshot_interval_seconds": SOAK_SNAPSHOT_INTERVAL_SECONDS,
        "snapshots": snapshots,
        "paper_identity_verified": True,
        "reconnect_count": 1,
        "unexpected_disconnect_count": 0,
        "continuity_gap_count": 0,
        "initial_state_checksum": "d" * 64,
        "final_state_checksum": "d" * 64,
        "final_reconciliation_exact": True,
        "state_unchanged": True,
        "rss_growth_bytes": 0,
        "maximum_shutdown_seconds": 0.1,
        "error_count": 0,
        "failed_stage": None,
        "failure_type": None,
        "passed": True,
    }


def _soak_reports() -> list[dict]:
    return [_soak_report(provider) for provider in ("alpaca", "ib")]


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


def test_qualification_order_is_eligible_during_ib_overnight_session() -> None:
    assert _tag_keyword("ib", "ml4tq-12345678-a") == {
        "order_ref": "ml4tq-12345678-a",
        "outsideRth": True,
    }
    assert _tag_keyword("alpaca", "ml4tq-12345678-a") == {"client_order_id": "ml4tq-12345678-a"}


@pytest.mark.asyncio
async def test_failed_provider_report_excludes_incomplete_stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    broker = MagicMock(unsafe=True)
    broker.is_connected = True
    broker.connect = AsyncMock()
    broker.disconnect = AsyncMock()
    broker.submit_order_async = AsyncMock(return_value=MagicMock(status=OrderStatus.PENDING))

    monkeypatch.setattr(paper_qualification, "_verify_installed_candidate", lambda *_args: None)
    monkeypatch.setattr(paper_qualification, "_build_broker", lambda _provider: broker)
    monkeypatch.setattr(paper_qualification, "_snapshot", AsyncMock(return_value={}))
    monkeypatch.setattr(paper_qualification, "_vendor_snapshot", lambda *_args: ({}, {}))
    monkeypatch.setattr(paper_qualification, "_assert_atomic_rejections", AsyncMock())
    monkeypatch.setattr(
        paper_qualification,
        "_wait_for_tag_count",
        AsyncMock(
            side_effect=PaperQualificationError(
                "provider did not reach the expected tagged-order state"
            )
        ),
    )
    monkeypatch.setattr(paper_qualification, "_cleanup_tags", AsyncMock(return_value=True))

    report = await run_provider_phase(
        provider="ib",
        phase="exercise",
        candidate=_candidate(),
        checkout_root=tmp_path,
        state_directory=tmp_path,
        tag_seed="12345678",
    )

    assert report["passed"] is False
    assert report["failed_stage"] == "working_acknowledgement"
    assert report["steps_passed"][-1] == "submit"
    assert "working_acknowledgement" not in report["steps_passed"]


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
    bundle = assemble_bundle(
        _candidate(),
        _reports(),
        _soak_reports(),
        generated_at="2026-08-08T18:00:02+00:00",
    )

    validate_bundle(bundle, expected_commit=COMMIT)
    assert bundle["candidate"]["wheel_sha256"] == WHEEL_HASH
    assert set(bundle["soaks"]) == {"alpaca", "ib"}
    assert bundle["passed"] is True


def test_bundle_rejects_report_for_different_wheel() -> None:
    reports = _reports()
    reports[0]["candidate"]["wheel_sha256"] = "d" * 64

    with pytest.raises(PaperQualificationError, match="different candidate"):
        assemble_bundle(_candidate(), reports, _soak_reports())


def test_bundle_rejects_missing_operation_or_cleanup() -> None:
    reports = _reports()
    reports[0]["steps_passed"].remove("replace")
    reports[0]["cleanup_passed"] = False

    with pytest.raises(PaperQualificationError):
        assemble_bundle(_candidate(), reports, _soak_reports())


def test_bundle_rejects_invalid_snapshot_instead_of_reporting_clean() -> None:
    reports = _reports()
    reports[-1]["snapshots"]["restart"]["pending_order_snapshot_exact"] = False

    with pytest.raises(PaperQualificationError, match="invalid snapshot"):
        assemble_bundle(_candidate(), reports, _soak_reports())


def test_retained_bundle_schema_has_no_account_or_order_identifiers() -> None:
    bundle = assemble_bundle(_candidate(), deepcopy(_reports()), deepcopy(_soak_reports()))
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
        assemble_bundle(_candidate(), reports, _soak_reports())


def test_bundle_rejects_missing_provider_soak() -> None:
    with pytest.raises(PaperQualificationError, match="six-hour soak"):
        assemble_bundle(_candidate(), _reports(), [_soak_report("alpaca")])


def test_bundle_rejects_duplicate_provider_soak() -> None:
    soaks = _soak_reports()
    soaks.append(deepcopy(soaks[0]))

    with pytest.raises(PaperQualificationError, match="six-hour soak"):
        assemble_bundle(_candidate(), _reports(), soaks)


def test_provider_soak_rejects_fabricated_snapshot_timeline() -> None:
    report = _soak_report("alpaca")
    for snapshot in report["snapshots"]:
        snapshot["elapsed_seconds"] = 0

    with pytest.raises(PaperQualificationError, match="snapshot continuity"):
        validate_provider_soak_report(report, _report("alpaca", "exercise")["candidate"], "alpaca")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("duration_seconds", SOAK_DURATION_SECONDS - 0.1),
        ("paper_identity_verified", False),
        ("reconnect_count", 0),
        ("unexpected_disconnect_count", 1),
        ("continuity_gap_count", 1),
        ("final_reconciliation_exact", False),
        ("state_unchanged", False),
        ("rss_growth_bytes", SOAK_RSS_GROWTH_LIMIT_BYTES),
        ("maximum_shutdown_seconds", 5.0),
        ("error_count", 1),
        ("failed_stage", "snapshot"),
        ("failure_type", "RuntimeError"),
        ("passed", False),
    ],
)
def test_provider_soak_rejects_incomplete_or_failed_evidence(field: str, value: object) -> None:
    report = _soak_report("alpaca")
    report[field] = value

    with pytest.raises(PaperQualificationError):
        validate_provider_soak_report(report, _report("alpaca", "exercise")["candidate"], "alpaca")


@pytest.mark.asyncio
async def test_provider_soak_runs_continuously_and_reconnects_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monotonic_time = 0.0
    woke_early = False

    async def fake_sleep(delay: float) -> None:
        nonlocal monotonic_time, woke_early
        if not woke_early and monotonic_time >= 0.3 and delay > 0.01:
            monotonic_time += delay - 0.01
            woke_early = True
        else:
            monotonic_time += delay

    class FakeBroker:
        def __init__(self) -> None:
            self.is_connected = False
            self.connect_count = 0
            self.disconnect_count = 0

        async def connect(self) -> None:
            self.is_connected = True
            self.connect_count += 1

        async def disconnect(self) -> None:
            self.is_connected = False
            self.disconnect_count += 1

        def assert_paper_trading(self) -> None:
            return None

    broker = FakeBroker()

    async def fake_snapshot(provider: str, captured_broker: object) -> dict:
        assert provider == "alpaca"
        assert captured_broker is broker
        return _snapshot()

    monkeypatch.setattr(paper_qualification, "SOAK_DURATION_SECONDS", 0.4)
    monkeypatch.setattr(paper_qualification, "SOAK_SNAPSHOT_INTERVAL_SECONDS", 0.1)
    monkeypatch.setattr(paper_qualification.time, "monotonic", lambda: monotonic_time)
    monkeypatch.setattr(paper_qualification.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(paper_qualification, "_verify_installed_candidate", lambda *_args: None)
    monkeypatch.setattr(paper_qualification, "_build_broker", lambda _provider: broker)
    monkeypatch.setattr(paper_qualification, "_snapshot", fake_snapshot)
    monkeypatch.setattr(paper_qualification, "_provider_state_checksum", lambda *_args: "d" * 64)

    report = await run_provider_soak(
        provider="alpaca", candidate=_candidate(), checkout_root=tmp_path
    )

    assert report["passed"] is True, {
        key: value for key, value in report.items() if key not in {"candidate", "snapshots"}
    }
    assert woke_early is True
    assert report["duration_seconds"] >= 0.4
    assert report["reconnect_count"] == 1
    assert len(report["snapshots"]) >= 5
    assert broker.connect_count == 2
    assert broker.disconnect_count == 2


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
