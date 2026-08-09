from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ml4t.backtest.types import Order, OrderSide, OrderStatus, OrderType, Position

from ml4t.live.brokers.alpaca import AlpacaBroker
from ml4t.live.brokers.ib import IBBroker
from ml4t.live.protocols import AsyncBrokerProtocol
from ml4t.live.safety import BrokerSnapshotError, LiveRiskConfig, SafeBroker

REQUIRED_CONCRETE_METHODS = {
    "assert_live_trading",
    "assert_paper_trading",
    "connect",
    "disconnect",
    "is_connected_async",
    "get_positions_async",
    "get_pending_orders_async",
    "get_position_async",
    "get_account_value_async",
    "get_cash_async",
    "submit_order_async",
    "cancel_order_async",
    "replace_order_async",
    "close_position_async",
}


def pending_order() -> Order:
    return Order(
        asset="SPY",
        side=OrderSide.BUY,
        quantity=10,
        order_type=OrderType.LIMIT,
        limit_price=500,
        order_id="venue-working-1",
        status=OrderStatus.PENDING,
        created_at=datetime.now(UTC),
    )


@pytest.mark.parametrize("adapter_type", [IBBroker, AlpacaBroker])
def test_real_adapter_declares_every_async_contract_method(adapter_type) -> None:
    assert AsyncBrokerProtocol not in adapter_type.__bases__
    assert REQUIRED_CONCRETE_METHODS <= adapter_type.__dict__.keys()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "broker",
    [
        IBBroker(),
        AlpacaBroker(api_key="PKTEST", secret_key="SECRET"),
    ],
)
async def test_real_adapter_async_connectivity_and_position_lookup_are_typed(broker) -> None:
    assert isinstance(broker, AsyncBrokerProtocol)
    position = Position(
        asset="SPY",
        quantity=10,
        entry_price=500,
        entry_time=datetime.now(UTC),
    )
    broker._positions["SPY"] = position

    assert await broker.is_connected_async() is False
    assert await broker.get_position_async("spy") is position
    assert await broker.get_position_async("MISSING") is None


@pytest.mark.asyncio
async def test_seeded_real_adapter_pending_order_is_a_blocking_mismatch(tmp_path) -> None:
    broker = IBBroker()
    broker._pending_orders["venue-working-1"] = pending_order()
    safe = SafeBroker(
        broker,
        LiveRiskConfig(
            execution_mode="paper",
            state_file=str(tmp_path / "state.json"),
            fail_on_reconciliation_mismatch=True,
        ),
    )

    report = await safe.preview_reconciliation_async()

    assert report["clean"] is False
    assert report["live_pending_orders"] == [
        {
            "asset": "SPY",
            "side": "buy",
            "quantity": 10.0,
            "order_type": "limit",
            "limit_price": 500.0,
        }
    ]
    assert report["unexpected_pending_orders"] == report["live_pending_orders"]


class InvalidSnapshotBroker:
    def __init__(self, *, positions: Any = None, pending: Any = None) -> None:
        self.positions_result = positions
        self.pending_result = pending
        self.connected = False

    @property
    def positions(self):
        return {}

    @property
    def pending_orders(self):
        return []

    async def get_positions_async(self):
        if isinstance(self.positions_result, Exception):
            raise self.positions_result
        return self.positions_result

    async def get_pending_orders_async(self, asset: str | None = None):
        if isinstance(self.pending_result, Exception):
            raise self.pending_result
        return self.pending_result

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def is_connected_async(self) -> bool:
        return self.connected

    def assert_paper_trading(self) -> None:
        """Identify this deterministic adapter as a paper venue."""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("positions", "pending", "match"),
    [
        (None, [], "positions snapshot"),
        ({}, None, "pending-orders snapshot"),
        ([], [], "positions snapshot"),
        ({}, {}, "pending-orders snapshot"),
        (RuntimeError("position API failed"), [], "position API failed"),
        ({}, RuntimeError("order API failed"), "order API failed"),
    ],
)
async def test_invalid_or_failed_snapshot_never_becomes_clean(
    tmp_path,
    positions,
    pending,
    match: str,
) -> None:
    safe = SafeBroker(
        cast(
            AsyncBrokerProtocol,
            InvalidSnapshotBroker(positions=positions, pending=pending),
        ),
        LiveRiskConfig(execution_mode="paper", state_file=str(tmp_path / "state.json")),
    )

    with pytest.raises(BrokerSnapshotError, match=match):
        await safe.preview_reconciliation_async()


@pytest.mark.asyncio
async def test_invalid_snapshot_disconnects_before_connect_returns(tmp_path) -> None:
    broker = InvalidSnapshotBroker(positions=None, pending=[])
    safe = SafeBroker(
        cast(AsyncBrokerProtocol, broker),
        LiveRiskConfig(execution_mode="paper", state_file=str(tmp_path / "state.json")),
    )

    with pytest.raises(BrokerSnapshotError, match="positions snapshot"):
        await safe.connect()

    assert broker.connected is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "order",
    [
        object(),
        Order(
            asset="",
            side=OrderSide.BUY,
            quantity=10,
            order_id="order-1",
            status=OrderStatus.PENDING,
            created_at=datetime.now(UTC),
        ),
        Order(
            asset="SPY",
            side=OrderSide.BUY,
            quantity=float("nan"),
            order_id="order-1",
            status=OrderStatus.PENDING,
            created_at=datetime.now(UTC),
        ),
        Order(
            asset="SPY",
            side=OrderSide.BUY,
            quantity=10,
            order_id="",
            status=OrderStatus.PENDING,
            created_at=datetime.now(UTC),
        ),
        Order(
            asset="SPY",
            side=OrderSide.BUY,
            quantity=10,
            order_id="order-1",
            status=OrderStatus.FILLED,
            created_at=datetime.now(UTC),
        ),
        Order(
            asset="SPY",
            side=OrderSide.BUY,
            quantity=10,
            order_id="order-1",
            status=OrderStatus.PENDING,
            created_at=None,
        ),
    ],
)
async def test_invalid_pending_order_snapshot_is_unavailable(tmp_path, order) -> None:
    safe = SafeBroker(
        cast(
            AsyncBrokerProtocol,
            InvalidSnapshotBroker(positions={}, pending=[order]),
        ),
        LiveRiskConfig(execution_mode="paper", state_file=str(tmp_path / "state.json")),
    )

    with pytest.raises(BrokerSnapshotError, match="pending-orders snapshot"):
        await safe.preview_reconciliation_async()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "position",
    [
        object(),
        Position("SPY", float("nan"), 500, datetime.now(UTC)),
        Position("SPY", 1, -1, datetime.now(UTC)),
        Position("SPY", 1, 500, datetime.now()),
        Position("SPY", 1, 500, datetime.now(UTC), current_price=-1),
        Position("SPY", 1, 500, datetime.now(UTC), current_price=float("inf")),
    ],
)
async def test_invalid_position_snapshot_is_unavailable(tmp_path, position) -> None:
    safe = SafeBroker(
        cast(
            AsyncBrokerProtocol,
            InvalidSnapshotBroker(positions={"SPY": position}, pending=[]),
        ),
        LiveRiskConfig(execution_mode="paper", state_file=str(tmp_path / "state.json")),
    )

    with pytest.raises(BrokerSnapshotError, match="positions snapshot"):
        await safe.preview_reconciliation_async()


@pytest.mark.asyncio
async def test_zero_valued_position_snapshot_is_available(tmp_path) -> None:
    position = Position("SPY", 1, 0, datetime.now(UTC), current_price=0)
    safe = SafeBroker(
        cast(
            AsyncBrokerProtocol,
            InvalidSnapshotBroker(positions={"SPY": position}, pending=[]),
        ),
        LiveRiskConfig(execution_mode="paper", state_file=str(tmp_path / "state.json")),
    )

    report = await safe.preview_reconciliation_async()

    assert report["live_positions"] == {"SPY": 1.0}


@pytest.mark.asyncio
async def test_ib_rejects_missing_or_unmanaged_account_identity() -> None:
    broker = IBBroker(account="DU-NOT-MANAGED")
    ib_client = cast(Any, broker.ib)
    with (
        patch.object(ib_client, "connectAsync", AsyncMock()),
        patch.object(ib_client, "managedAccounts", return_value=["DU-MANAGED"]),
    ):
        with pytest.raises(RuntimeError, match="account"):
            await broker.connect()

    assert broker._connected is False


@pytest.mark.asyncio
async def test_alpaca_sync_failure_aborts_connection_without_empty_state(monkeypatch) -> None:
    account = MagicMock(
        equity="100000",
        cash="50000",
        account_number="PA-TEST",
    )
    client = MagicMock()
    client.get_account.return_value = account
    client.get_all_positions.side_effect = RuntimeError("positions unavailable")
    stream = MagicMock()
    monkeypatch.setattr("ml4t.live.brokers.alpaca.TradingClient", lambda **kwargs: client)
    monkeypatch.setattr("ml4t.live.brokers.alpaca.TradingStream", lambda **kwargs: stream)
    broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")

    with pytest.raises(RuntimeError, match="positions unavailable"):
        await broker.connect()

    assert broker._connected is False
    assert broker.positions == {}
    stream.stop.assert_not_called()


def ib_update(order_id: int, status: str, filled: float, price: float = 0.0) -> MagicMock:
    update = MagicMock()
    update.order.orderId = order_id
    update.orderStatus.status = status
    update.orderStatus.filled = filled
    update.orderStatus.avgFillPrice = price
    return update


def alpaca_update(event: str, filled: float, price: float = 0.0) -> SimpleNamespace:
    return SimpleNamespace(
        event=event,
        order=SimpleNamespace(
            id="venue-1",
            filled_qty=str(filled),
            filled_avg_price=str(price) if price else None,
        ),
    )


def seed_ib_order() -> tuple[IBBroker, Order]:
    broker = IBBroker()
    order = pending_order()
    broker._pending_orders[order.order_id] = order
    broker._ib_order_map[1] = (order.order_id, 0.0)
    return broker, order


def seed_alpaca_order() -> tuple[AlpacaBroker, Order]:
    broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
    order = pending_order()
    broker._pending_orders[order.order_id] = order
    broker._alpaca_order_map["venue-1"] = (order.order_id, 0.0)
    return broker, order


def test_ib_order_updates_are_monotonic_and_terminal() -> None:
    broker, order = seed_ib_order()

    broker._on_order_status(ib_update(1, "Submitted", 4, 499))
    broker._on_order_status(ib_update(1, "Submitted", 4, 499))
    broker._on_order_status(ib_update(1, "Submitted", 2, 498))
    broker._on_order_status(ib_update(1, "PendingCancel", 4, 499))
    broker._on_order_status(ib_update(1, "Filled", 10, 500))
    broker._on_order_status(ib_update(1, "Cancelled", 10, 500))

    assert order.status is OrderStatus.FILLED
    assert order.filled_quantity == 10
    assert order.filled_price == 500
    assert order.order_id not in broker._pending_orders


@pytest.mark.parametrize(
    ("status", "expected"),
    [("Inactive", OrderStatus.REJECTED), ("ApiCancelled", OrderStatus.CANCELLED)],
)
def test_ib_reject_and_cancel_are_terminal(status: str, expected: OrderStatus) -> None:
    broker, order = seed_ib_order()

    broker._on_order_status(ib_update(1, status, 0))
    broker._on_order_status(ib_update(1, "Submitted", 0))

    assert order.status is expected
    assert order.order_id not in broker._pending_orders
    assert 1 not in broker._ib_order_map


@pytest.mark.asyncio
async def test_alpaca_order_updates_are_monotonic_across_cancel_race() -> None:
    broker, order = seed_alpaca_order()
    broker._trading_client = MagicMock()
    broker._trading_client.get_all_positions.return_value = []

    await broker._on_trade_update(alpaca_update("partial_fill", 4, 499))
    await broker._on_trade_update(alpaca_update("partial_fill", 4, 499))
    await broker._on_trade_update(alpaca_update("partial_fill", 2, 498))
    await broker._on_trade_update(alpaca_update("pending_cancel", 4, 499))
    await broker._on_trade_update(alpaca_update("fill", 10, 500))
    await broker._on_trade_update(alpaca_update("canceled", 10, 500))

    assert order.status is OrderStatus.FILLED
    assert order.filled_quantity == 10
    assert order.filled_price == 500
    assert order.order_id not in broker._pending_orders


@pytest.mark.asyncio
async def test_alpaca_replace_rejection_keeps_order_live_until_terminal_fill() -> None:
    broker, order = seed_alpaca_order()
    broker._trading_client = MagicMock()
    broker._trading_client.get_all_positions.return_value = []

    await broker._on_trade_update(alpaca_update("pending_replace", 0))
    await broker._on_trade_update(alpaca_update("order_replace_rejected", 0))
    assert broker._pending_orders[order.order_id] is order

    await broker._on_trade_update(alpaca_update("fill", 10, 501))
    assert order.status is OrderStatus.FILLED
    assert order.order_id not in broker._pending_orders


@pytest.mark.asyncio
async def test_unknown_alpaca_order_event_marks_snapshot_unavailable() -> None:
    broker, _ = seed_alpaca_order()

    await broker._on_trade_update(alpaca_update("vendor_added_event", 0))

    with pytest.raises(RuntimeError, match="unsupported Alpaca order event"):
        await broker.get_pending_orders_async()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event", "expected"),
    [("rejected", OrderStatus.REJECTED), ("canceled", OrderStatus.CANCELLED)],
)
async def test_alpaca_reject_and_cancel_are_terminal(event: str, expected: OrderStatus) -> None:
    broker, order = seed_alpaca_order()

    await broker._on_trade_update(alpaca_update(event, 0))
    await broker._on_trade_update(alpaca_update("partial_fill", 4, 499))

    assert order.status is expected
    assert order.order_id not in broker._pending_orders
    assert "venue-1" not in broker._alpaca_order_map


@pytest.mark.asyncio
async def test_invalid_ib_position_sync_is_atomic_and_marks_snapshot_unavailable() -> None:
    broker = IBBroker()
    existing = Position("SPY", 1, 500, datetime.now(UTC))
    broker._positions = {"SPY": existing}
    with patch.object(
        cast(Any, broker.ib),
        "reqPositionsAsync",
        AsyncMock(
            return_value=[
                SimpleNamespace(
                    contract=SimpleNamespace(symbol="AAPL"),
                    position=1,
                    avgCost=float("nan"),
                )
            ]
        ),
    ):
        with pytest.raises(RuntimeError, match="average cost"):
            await broker._sync_positions()
    with pytest.raises(RuntimeError, match="average cost"):
        await broker.get_positions_async()

    assert broker._positions == {"SPY": existing}


@pytest.mark.asyncio
async def test_account_metrics_never_substitute_zero_for_unavailable_state() -> None:
    ib = IBBroker(account="DU-1")
    with patch.object(cast(Any, ib.ib), "accountValues", return_value=[]):
        with pytest.raises(RuntimeError, match="NetLiquidation"):
            await ib.get_account_value_async()

    alpaca = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
    with pytest.raises(RuntimeError, match="unavailable"):
        await alpaca.get_cash_async()
