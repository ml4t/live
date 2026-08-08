from __future__ import annotations

import json
import os
import stat
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ml4t.backtest.types import Order, OrderSide, OrderStatus, OrderType, Position

from ml4t.live import (
    AcceptedOrderPersistenceError,
    AuditJournalError,
    ConcurrentStateWriterError,
    CorruptStateError,
    LiveRiskConfig,
    PersistenceSafetyError,
    SafeBroker,
    UnsafePersistencePathError,
)
from ml4t.live.brokers.alpaca import AlpacaBroker
from ml4t.live.brokers.ib import IBBroker
from ml4t.live.persistence import SecureAuditJournal, SecureStateStore


class PersistenceBroker:
    def __init__(self) -> None:
        self.positions: dict[str, Position] = {}
        self.pending_orders: list[Order] = []
        self.submit_calls = 0

    def get_position(self, asset: str) -> Position | None:
        return self.positions.get(asset.upper())

    async def get_account_value_async(self) -> float:
        return 100_000.0

    async def get_cash_async(self) -> float:
        return 100_000.0

    async def submit_order_async(
        self,
        asset: str,
        quantity: float,
        side: OrderSide | None = None,
        order_type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
        stop_price: float | None = None,
        **kwargs,
    ) -> Order:
        self.submit_calls += 1
        order = Order(
            asset=asset,
            quantity=quantity,
            side=side or OrderSide.BUY,
            order_type=order_type,
            limit_price=limit_price,
            stop_price=stop_price,
            order_id=f"venue-{self.submit_calls}",
            status=OrderStatus.PENDING,
            created_at=datetime.now(UTC),
        )
        self.pending_orders.append(order)
        return order


def config(root: Path, **overrides) -> LiveRiskConfig:
    values = {
        "state_file": str(root / "state.json"),
        "journal_file": str(root / "journal.jsonl"),
        "max_data_staleness_seconds": None,
        "max_daily_loss": None,
        "max_drawdown_pct": None,
    }
    values.update(overrides)
    return LiveRiskConfig(**values)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.parametrize("mask", [0o0000, 0o0002])
def test_state_journal_and_locks_remain_owner_only_after_replacement(tmp_path, mask):
    previous = os.umask(mask)
    safe = None
    try:
        safe = SafeBroker(PersistenceBroker(), config(tmp_path))
        safe._save_state()
        safe._state.orders_placed = 1
        safe._save_state()
        safe.record_event("first")
        safe.record_event("second")
    finally:
        os.umask(previous)
        if safe is not None:
            safe.close_persistence()

    paths = [
        tmp_path / "state.json",
        tmp_path / "state.json.lock",
        tmp_path / "journal.jsonl",
        tmp_path / "journal.jsonl.head",
        tmp_path / "journal.jsonl.lock",
    ]
    assert {path.name: mode(path) for path in paths} == {path.name: 0o600 for path in paths}


def test_corrupt_state_fails_closed_and_preserves_diagnostic_bytes(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_bytes(b"{")
    state_path.chmod(0o600)
    before = state_path.read_bytes()
    broker = PersistenceBroker()

    with pytest.raises(CorruptStateError):
        SafeBroker(broker, config(tmp_path))

    assert state_path.read_bytes() == before
    assert broker.submit_calls == 0


def test_state_integrity_tamper_fails_closed(tmp_path):
    safe = SafeBroker(PersistenceBroker(), config(tmp_path))
    safe._state.orders_placed = 3
    safe._save_state()
    safe.close_persistence()
    path = tmp_path / "state.json"
    envelope = json.loads(path.read_text())
    envelope["payload"]["orders_placed"] = 99
    path.write_text(json.dumps(envelope))

    with pytest.raises(CorruptStateError, match="integrity"):
        SafeBroker(PersistenceBroker(), config(tmp_path))


def test_incompatible_schema_fails_closed(tmp_path):
    safe = SafeBroker(PersistenceBroker(), config(tmp_path))
    safe._save_state()
    safe.close_persistence()
    path = tmp_path / "state.json"
    envelope = json.loads(path.read_text())
    envelope["schema_version"] = 999
    path.write_text(json.dumps(envelope))

    with pytest.raises(CorruptStateError, match="schema version"):
        SafeBroker(PersistenceBroker(), config(tmp_path))


def test_invalid_state_content_type_fails_closed(tmp_path):
    payload = {
        "date": date.today().isoformat(),
        "daily_loss": "not-a-number",
        "orders_placed": 0,
        "high_water_mark": 0.0,
        "kill_switch_activated": False,
        "kill_switch_reason": "",
    }
    SecureStateStore(tmp_path / "state.json").save(payload, expected_generation=0)

    with pytest.raises(CorruptStateError, match="daily_loss"):
        SafeBroker(PersistenceBroker(), config(tmp_path))


def test_legacy_state_is_validated_and_migrated_immediately(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "date": date.today().isoformat(),
                "daily_loss": 125.0,
                "orders_placed": 2,
                "high_water_mark": 100_000.0,
                "kill_switch_activated": False,
                "kill_switch_reason": "",
            }
        )
    )
    path.chmod(0o600)

    safe = SafeBroker(PersistenceBroker(), config(tmp_path))
    envelope = json.loads(path.read_text())

    assert safe._state.daily_loss == 125.0
    assert envelope["schema_version"] == 1
    assert envelope["generation"] == 1
    safe.close_persistence()


def test_wrong_mode_state_fails_closed(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{}")
    path.chmod(0o644)

    with pytest.raises(UnsafePersistencePathError, match="mode 0600"):
        SafeBroker(PersistenceBroker(), config(tmp_path))


def test_state_symlink_fails_closed(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("{}")
    target.chmod(0o600)
    (tmp_path / "state.json").symlink_to(target)

    with pytest.raises(UnsafePersistencePathError):
        SafeBroker(PersistenceBroker(), config(tmp_path))

    assert target.read_text() == "{}"


def test_symlinked_parent_fails_closed(tmp_path):
    target = tmp_path / "real-parent"
    target.mkdir()
    linked = tmp_path / "linked-parent"
    linked.symlink_to(target, target_is_directory=True)

    with pytest.raises(UnsafePersistencePathError, match="parent"):
        SafeBroker(PersistenceBroker(), config(linked))


def test_wrong_owner_state_fails_closed(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{}")
    path.chmod(0o600)
    store = SecureStateStore(path)
    store.acquire_writer()
    try:
        with (
            patch("ml4t.live.persistence.os.geteuid", return_value=path.stat().st_uid + 1),
            pytest.raises(UnsafePersistencePathError, match="owned"),
        ):
            store.load()
    finally:
        store.release_writer()


def test_second_writer_is_rejected_before_state_or_broker_mutation(tmp_path):
    first = SafeBroker(PersistenceBroker(), config(tmp_path))
    second_broker = PersistenceBroker()

    with pytest.raises(ConcurrentStateWriterError):
        SafeBroker(second_broker, config(tmp_path))

    assert second_broker.submit_calls == 0
    first.close_persistence()


class SimulatedCrash(BaseException):
    pass


@pytest.mark.parametrize(
    ("boundary", "expected_orders"),
    [
        ("after_temp_fsync", 1),
        ("after_replace", 2),
        ("after_directory_fsync", 2),
    ],
)
def test_state_crash_boundaries_leave_old_or_new_complete_state(
    tmp_path, boundary, expected_orders
):
    path = tmp_path / f"{boundary}.json"
    old_payload = {
        "date": date.today().isoformat(),
        "daily_loss": 0.0,
        "orders_placed": 1,
        "high_water_mark": 0.0,
        "kill_switch_activated": False,
        "kill_switch_reason": "",
    }
    SecureStateStore(path).save(old_payload, expected_generation=0)
    new_payload = {**old_payload, "orders_placed": 2}

    def inject(stage: str) -> None:
        if stage == boundary:
            raise SimulatedCrash

    with pytest.raises(SimulatedCrash):
        SecureStateStore(path, fault_injector=inject).save(new_payload, expected_generation=1)

    snapshot = SecureStateStore(path).load()
    assert snapshot is not None
    assert snapshot.payload["orders_placed"] == expected_orders


@pytest.mark.asyncio
async def test_required_journal_failure_blocks_order_before_venue_call(tmp_path):
    broker = PersistenceBroker()
    safe = SafeBroker(broker, config(tmp_path))
    safe.record_market_snapshot("SPY", 100.0)
    (tmp_path / "journal.jsonl").mkdir()

    with pytest.raises(AuditJournalError):
        await safe.submit_order_async("SPY", 1)

    assert broker.submit_calls == 0
    assert safe.persistence_status["journal_error"] == "AuditJournalError"
    safe.close_persistence()


@pytest.mark.asyncio
async def test_explicit_best_effort_journal_policy_is_observable(tmp_path):
    journal = tmp_path / "journal.jsonl"
    journal.mkdir()
    broker = PersistenceBroker()
    safe = SafeBroker(broker, config(tmp_path, fail_on_journal_error=False))
    safe.record_market_snapshot("SPY", 100.0)

    await safe.submit_order_async("SPY", 1)

    assert broker.submit_calls == 1
    assert safe.persistence_status == {
        "state_schema_version": 1,
        "state_generation": 1,
        "state_error": None,
        "journal_required": False,
        "journal_error": "AuditJournalError",
    }
    safe.close_persistence()


@pytest.mark.asyncio
async def test_accepted_order_state_failure_is_explicit_and_sticky(tmp_path, monkeypatch):
    broker = PersistenceBroker()
    safe = SafeBroker(broker, config(tmp_path))
    safe.record_market_snapshot("SPY", 100.0)

    def fail_after_intent(*args, **kwargs):
        raise PersistenceSafetyError("injected state failure")

    monkeypatch.setattr(safe._state_store, "save", fail_after_intent)
    with pytest.raises(AcceptedOrderPersistenceError, match="do not retry"):
        await safe.submit_order_async("SPY", 1)

    assert broker.submit_calls == 1
    assert safe.persistence_status["state_error"] == "PersistenceSafetyError"
    with pytest.raises(PersistenceSafetyError):
        await safe.submit_order_async("SPY", 1)
    assert broker.submit_calls == 1
    safe.close_persistence()


@pytest.mark.asyncio
async def test_accepted_order_journal_completion_failure_is_explicit_and_sticky(
    tmp_path, monkeypatch
):
    broker = PersistenceBroker()
    safe = SafeBroker(broker, config(tmp_path))
    safe.record_market_snapshot("SPY", 100.0)
    append = safe._audit_journal.append

    def fail_completion(event):
        if event["event"] == "order_submitted":
            raise AuditJournalError("injected completion failure")
        return append(event)

    monkeypatch.setattr(safe._audit_journal, "append", fail_completion)
    with pytest.raises(AcceptedOrderPersistenceError, match="do not retry"):
        await safe.submit_order_async("SPY", 1)

    assert broker.submit_calls == 1
    assert safe.persistence_status["journal_error"] == "AuditJournalError"
    with pytest.raises(AuditJournalError):
        await safe.submit_order_async("SPY", 1)
    assert broker.submit_calls == 1
    safe.close_persistence()


def test_journal_tamper_and_truncation_fail_closed(tmp_path):
    safe = SafeBroker(PersistenceBroker(), config(tmp_path))
    safe.record_event("one")
    safe.record_event("two")
    safe.close_persistence()
    journal = tmp_path / "journal.jsonl"
    original = journal.read_bytes()

    lines = journal.read_text().splitlines()
    first = json.loads(lines[0])
    first["event"] = "tampered"
    journal.write_text("\n".join([json.dumps(first), *lines[1:]]) + "\n")
    with pytest.raises(AuditJournalError, match="hash chain"):
        SafeBroker(PersistenceBroker(), config(tmp_path))

    journal.write_bytes(original[:-1])
    journal.chmod(0o600)
    with pytest.raises(AuditJournalError, match="truncated"):
        SafeBroker(PersistenceBroker(), config(tmp_path))

    journal.write_bytes(original.splitlines(keepends=True)[0])
    journal.chmod(0o600)
    with pytest.raises(AuditJournalError, match="truncated below"):
        SafeBroker(PersistenceBroker(), config(tmp_path))

    journal.write_bytes(original)
    (tmp_path / "journal.jsonl.head").unlink()
    with pytest.raises(AuditJournalError, match="head is missing"):
        SafeBroker(PersistenceBroker(), config(tmp_path))


def test_journal_symlink_and_wrong_mode_fail_closed(tmp_path):
    target = tmp_path / "journal-target.jsonl"
    target.write_text("")
    target.chmod(0o600)
    journal = tmp_path / "journal.jsonl"
    journal.symlink_to(target)

    with pytest.raises(AuditJournalError):
        SafeBroker(PersistenceBroker(), config(tmp_path))


def test_direct_journal_append_rejects_wrong_mode_without_repairing_it(tmp_path):
    journal = tmp_path / "journal.jsonl"
    journal.write_text("")
    journal.chmod(0o644)

    with pytest.raises(AuditJournalError):
        SecureAuditJournal(journal).append({"event": "must-not-be-written"})

    assert journal.read_text() == ""
    assert mode(journal) == 0o644

    journal.unlink()
    journal.write_text("")
    journal.chmod(0o644)
    with pytest.raises(AuditJournalError):
        SafeBroker(PersistenceBroker(), config(tmp_path))


@pytest.mark.parametrize(
    "name",
    ["state.json.lock", "journal.jsonl.lock", "journal.jsonl.head"],
)
def test_wrong_mode_persistence_sidecar_fails_closed(tmp_path, name):
    safe = SafeBroker(PersistenceBroker(), config(tmp_path))
    safe._save_state()
    safe.record_event("probe")
    safe.close_persistence()
    (tmp_path / name).chmod(0o644)

    expected = UnsafePersistencePathError if name == "state.json.lock" else AuditJournalError
    with pytest.raises(expected):
        SafeBroker(PersistenceBroker(), config(tmp_path))


def test_journal_redacts_credentials_accounts_and_exception_text(tmp_path):
    safe = SafeBroker(PersistenceBroker(), config(tmp_path))
    safe.record_event(
        "failure",
        account_id="DU1234567",
        api_key="PKSUPERSECRET123",
        error=RuntimeError("Bearer top-secret secret_key=hidden DU7654321"),
    )
    content = (tmp_path / "journal.jsonl").read_text()
    entry = json.loads(content)

    assert "DU1234567" not in content
    assert "DU7654321" not in content
    assert "PKSUPERSECRET123" not in content
    assert "top-secret" not in content
    assert "hidden" not in content
    assert entry["payload"]["account_id"] == "[REDACTED]"
    assert entry["payload"]["api_key"] == "[REDACTED]"
    safe.close_persistence()


@pytest.mark.asyncio
async def test_alpaca_connection_exception_and_log_are_redacted(caplog):
    secret = "PKSUPERSECRET123"
    account = "DU7654321"
    error = RuntimeError(f"api_key={secret} account={account} Bearer hidden-token")

    with patch("ml4t.live.brokers.alpaca.TradingClient", side_effect=error):
        broker = AlpacaBroker(api_key=secret, secret_key="ALPACA-SECRET")
        with pytest.raises(RuntimeError) as captured:
            await broker.connect()

    retained = caplog.text + str(captured.value)
    assert secret not in retained
    assert account not in retained
    assert "hidden-token" not in retained
    assert "[REDACTED]" in retained


@pytest.mark.asyncio
async def test_ib_connection_exception_and_log_are_redacted(caplog):
    secret = "PKIBSUPERSECRET123"
    account = "DU1234567"
    vendor = MagicMock()
    vendor.connectAsync = AsyncMock(side_effect=RuntimeError(f"token={secret} account={account}"))
    broker = IBBroker(account=account)
    broker.ib = vendor

    with pytest.raises(RuntimeError) as captured:
        await broker.connect()

    retained = caplog.text + str(captured.value)
    assert secret not in retained
    assert account not in retained
    assert "[REDACTED]" in retained
