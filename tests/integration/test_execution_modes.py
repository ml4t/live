"""Public-boundary execution-mode safety tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from ml4t.backtest.types import Order, OrderSide, OrderStatus, OrderType, Position

from ml4t.live import ExecutionModeError, LiveRiskConfig, SafeBroker

pytestmark = [pytest.mark.integration, pytest.mark.deterministic]


class ModeBroker:
    def __init__(self, identity: str) -> None:
        self.identity = identity
        self._connected = False
        self.submit_calls = 0
        self.disconnect_calls = 0
        self._positions: dict[str, Position] = {}
        self._pending_orders: list[Order] = []

    @property
    def positions(self) -> dict[str, Position]:
        return dict(self._positions)

    @property
    def pending_orders(self) -> list[Order]:
        return list(self._pending_orders)

    @property
    def execution_capabilities(self) -> frozenset[str]:
        return frozenset()

    def get_position(self, asset: str) -> Position | None:
        return self._positions.get(asset.upper())

    def assert_paper_trading(self) -> None:
        if not self._connected or self.identity != "paper":
            raise RuntimeError("not connected to a paper account")

    def assert_live_trading(self) -> None:
        if not self._connected or self.identity != "live":
            raise RuntimeError("not connected to a live account")

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._connected = False

    async def is_connected_async(self) -> bool:
        return self._connected

    async def get_positions_async(self) -> dict[str, Position]:
        return self.positions

    async def get_pending_orders_async(self, asset: str | None = None) -> list[Order]:
        if asset is None:
            return self.pending_orders
        return [order for order in self._pending_orders if order.asset == asset.upper()]

    async def get_position_async(self, asset: str) -> Position | None:
        return self.get_position(asset)

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
        **kwargs: Any,
    ) -> Order:
        self.submit_calls += 1
        return Order(
            asset=asset,
            quantity=quantity,
            side=side or OrderSide.BUY,
            order_type=order_type,
            order_id=f"mode-{self.submit_calls}",
            status=OrderStatus.PENDING,
            created_at=datetime.now(UTC),
        )

    async def cancel_order_async(self, order_id: str) -> bool:
        return False

    async def replace_order_async(self, order_id: str, **kwargs: Any) -> Order:
        raise NotImplementedError

    async def close_position_async(self, asset: str) -> Order | None:
        return None


def test_ambiguous_execution_mode_fails_before_state_or_provider_mutation(tmp_path) -> None:
    raw_broker = MagicMock()
    raw_broker.connect = AsyncMock()
    raw_broker.submit_order_async = AsyncMock()
    state_file = tmp_path / "risk-state.json"

    with pytest.raises(ValueError, match="execution_mode"):
        SafeBroker(raw_broker, LiveRiskConfig(state_file=str(state_file)))

    raw_broker.connect.assert_not_called()
    raw_broker.submit_order_async.assert_not_called()
    assert not state_file.exists()
    assert not state_file.with_name(f"{state_file.name}.lock").exists()
    assert not state_file.with_name("risk-state-journal.jsonl").exists()


@pytest.mark.asyncio
async def test_shadow_mode_never_calls_provider_order_api(tmp_path) -> None:
    broker = ModeBroker("live")
    safe = SafeBroker(
        broker,
        LiveRiskConfig(
            execution_mode="shadow",
            state_file=str(tmp_path / "shadow-state.json"),
        ),
    )
    safe.record_market_snapshot("SPY", 100.0)

    order = await safe.submit_order_async("SPY", 1)

    assert order.status is OrderStatus.FILLED
    assert broker.submit_calls == 0
    assert safe.persistence_status["execution_mode"] == "shadow"
    assert safe.persistence_status["execution_identity_validated"] is True
    safe.close_persistence()


def test_disabled_safety_controls_are_visible_in_status_and_logs(tmp_path, caplog) -> None:
    with caplog.at_level("WARNING"):
        safe = SafeBroker(
            ModeBroker("paper"),
            LiveRiskConfig(
                execution_mode="shadow",
                max_daily_loss=None,
                max_drawdown_pct=None,
                state_file=str(tmp_path / "shadow-state.json"),
            ),
        )

    assert safe.persistence_status["disabled_safety_controls"] == [
        "max_daily_loss",
        "max_drawdown_pct",
    ]
    assert "Disabled safety controls: max_daily_loss, max_drawdown_pct" in caplog.text
    safe.close_persistence()


@pytest.mark.asyncio
async def test_paper_mode_rejects_live_identity_and_disconnects(tmp_path) -> None:
    broker = ModeBroker("live")
    safe = SafeBroker(
        broker,
        LiveRiskConfig(
            execution_mode="paper",
            state_file=str(tmp_path / "paper-state.json"),
        ),
    )

    with pytest.raises(ExecutionModeError, match="execution_mode=paper"):
        await safe.connect()

    assert broker._connected is False
    assert broker.disconnect_calls == 1
    assert broker.submit_calls == 0
    assert safe.persistence_status["execution_identity_validated"] is False
    safe.close_persistence()


@pytest.mark.asyncio
@pytest.mark.parametrize("execution_mode", ["paper", "live"])
async def test_external_mode_identity_is_retained_in_status(tmp_path, execution_mode) -> None:
    broker = ModeBroker(execution_mode)
    safe = SafeBroker(
        broker,
        LiveRiskConfig(
            execution_mode=execution_mode,
            state_file=str(tmp_path / f"{execution_mode}-state.json"),
        ),
    )

    await safe.connect()

    assert safe.persistence_status["execution_mode"] == execution_mode
    assert safe.persistence_status["execution_identity_validated"] is True
    assert safe._state.execution_mode == execution_mode
    assert broker.submit_calls == 0
    await safe.disconnect()


@pytest.mark.asyncio
async def test_external_order_revalidates_identity_before_mutation(tmp_path) -> None:
    broker = ModeBroker("paper")
    state_file = tmp_path / "paper-state.json"
    safe = SafeBroker(
        broker,
        LiveRiskConfig(execution_mode="paper", state_file=str(state_file)),
    )
    safe.record_market_snapshot("SPY", 100.0)

    with pytest.raises(ExecutionModeError, match="execution_mode=paper"):
        await safe.submit_order_async("SPY", 1)

    assert broker.submit_calls == 0
    assert safe._state.orders_placed == 0
    assert not state_file.exists()
    safe.close_persistence()


def test_persisted_execution_mode_cannot_be_reused_for_another_destination(tmp_path) -> None:
    state_file = tmp_path / "risk-state.json"
    shadow = SafeBroker(
        ModeBroker("paper"),
        LiveRiskConfig(execution_mode="shadow", state_file=str(state_file)),
    )
    shadow._save_state()
    shadow.close_persistence()

    with pytest.raises(ExecutionModeError, match="persisted execution mode"):
        SafeBroker(
            ModeBroker("paper"),
            LiveRiskConfig(execution_mode="paper", state_file=str(state_file)),
        )
