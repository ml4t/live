"""End-to-end upgrades from retained beta persistence fixtures."""

from __future__ import annotations

import json
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
from ml4t.backtest import Strategy

from ml4t.live import AuditJournalError, CorruptStateError, LiveEngine, LiveRiskConfig, SafeBroker
from ml4t.live.persistence import SecureStateStore

FIXTURES = Path(__file__).parents[1] / "fixtures" / "migration"


class MigrationBroker:
    def __init__(self) -> None:
        self.positions = {}
        self.pending_orders = []
        self.connect_calls = 0
        self.submit_calls = 0

    def assert_paper_trading(self) -> None:
        """Identify the fixture as a paper account."""

    def get_position(self, asset: str):
        return self.positions.get(asset)


class FixedDate(date):
    @classmethod
    def today(cls) -> FixedDate:
        return cls(2026, 8, 9)


class NoOpStrategy(Strategy):
    def on_data(
        self,
        timestamp: datetime,
        data: dict[str, dict],
        context: dict[str, Any],
        broker: Any,
    ) -> None:
        pass


def migration_config(root: Path) -> LiveRiskConfig:
    return LiveRiskConfig(
        execution_mode="paper",
        state_file=str(root / "state.json"),
        journal_file=str(root / "journal.jsonl"),
        max_data_staleness_seconds=None,
    )


def install_fixture(root: Path, name: str, destination: str) -> Path:
    path = root / destination
    shutil.copyfile(FIXTURES / name, path)
    path.chmod(0o600)
    return path


def test_published_beta_state_upgrades_without_losing_safety_fields(tmp_path: Path) -> None:
    state_path = install_fixture(tmp_path, "0.1.0b3-risk-state.json", "state.json")
    broker = MigrationBroker()

    with patch("ml4t.live.safety.date", FixedDate):
        safe = SafeBroker(cast(Any, broker), migration_config(tmp_path))
    safe.close_persistence()

    envelope = json.loads(state_path.read_text())
    payload = envelope["payload"]
    assert envelope["schema_version"] == 1
    assert payload["daily_loss"] == 75.25
    assert payload["orders_placed"] == 4
    assert payload["persisted_positions"] == {"SPY": 50.0}
    assert payload["persisted_pending_orders"][0]["limit_price"] == 105.0
    assert payload["kill_switch_activated"] is True
    assert payload["kill_switch_reason"] == "published beta operator halt"
    assert payload["execution_mode"] == "paper"
    assert broker.connect_calls == broker.submit_calls == 0


def test_qualified_beta_state_and_journal_upgrade_as_one_transaction(tmp_path: Path) -> None:
    state_path = install_fixture(tmp_path, "0.1.0b4-risk-state.json", "state.json")
    journal_path = install_fixture(tmp_path, "0.1.0b4-journal.jsonl", "journal.jsonl")
    install_fixture(tmp_path, "0.1.0b4-journal.jsonl.head", "journal.jsonl.head")
    journal_before = journal_path.read_bytes()
    broker = MigrationBroker()

    with patch("ml4t.live.safety.date", FixedDate):
        safe = SafeBroker(cast(Any, broker), migration_config(tmp_path))
        engine = LiveEngine(NoOpStrategy(), cast(Any, safe), cast(Any, object()))
    safe.close_persistence()

    envelope = json.loads(state_path.read_text())
    portable = envelope["payload"]["portable_strategy_state"]
    child = portable["children"][0]
    rule = portable["position_rule_states"][0]
    assert envelope["generation"] == 2
    assert envelope["payload"]["execution_mode"] == "paper"
    assert child["decision_session"] == child["effective_session"] == "2026-08-10"
    assert child["eligibility_phase"] == "pre_open"
    assert rule["rule_id"] == "stop-5"
    assert rule["entry_side"] == "buy"
    assert rule["context"] == {"atr": 2.5}
    assert rule["duration_events"] == 3
    assert engine.strategy_runtime.position_rule_states[0].high_water_mark == 104.0
    assert engine.strategy_runtime.reconciliations[0].remaining_quantity == 125.0
    assert journal_path.read_bytes() == journal_before
    assert broker.connect_calls == broker.submit_calls == 0


def test_invalid_beta_journal_rejects_before_legacy_state_replacement(tmp_path: Path) -> None:
    state_path = install_fixture(tmp_path, "0.1.0b3-risk-state.json", "state.json")
    before = state_path.read_bytes()
    journal = tmp_path / "journal.jsonl"
    journal.write_text('{"event":"unchained beta record"}\n')
    journal.chmod(0o600)
    broker = MigrationBroker()

    with (
        patch("ml4t.live.safety.date", FixedDate),
        pytest.raises(AuditJournalError, match="head is missing"),
    ):
        SafeBroker(cast(Any, broker), migration_config(tmp_path))

    assert state_path.read_bytes() == before
    assert broker.connect_calls == broker.submit_calls == 0


def test_future_portable_schema_rejects_before_state_replacement(tmp_path: Path) -> None:
    source = json.loads((FIXTURES / "0.1.0b4-risk-state.json").read_text())
    payload = source["payload"]
    payload["portable_strategy_state"]["schema_version"] = 999
    state_path = tmp_path / "state.json"
    store = SecureStateStore(state_path)
    store.save(payload, expected_generation=0)
    state_path.chmod(0o600)
    before = state_path.read_bytes()
    broker = MigrationBroker()

    with pytest.raises(CorruptStateError, match="portable strategy state schema"):
        SafeBroker(cast(Any, broker), migration_config(tmp_path))

    assert state_path.read_bytes() == before
    assert broker.connect_calls == broker.submit_calls == 0


def test_incomplete_current_portable_schema_rejects_without_repair(tmp_path: Path) -> None:
    source = json.loads((FIXTURES / "0.1.0b4-risk-state.json").read_text())
    payload = source["payload"]
    payload["portable_strategy_state"]["schema_version"] = 1
    state_path = tmp_path / "state.json"
    store = SecureStateStore(state_path)
    store.save(payload, expected_generation=0)
    before = state_path.read_bytes()

    with pytest.raises(CorruptStateError, match="portable strategy state is invalid"):
        SafeBroker(cast(Any, MigrationBroker()), migration_config(tmp_path))

    assert state_path.read_bytes() == before
