from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from alpaca.trading.enums import OrderStatus as AlpacaOrderStatus
from ml4t.backtest.types import Order, OrderSide, OrderStatus, OrderType, Position
from ml4t.specs import ExecutionCapability

from ml4t.live.brokers.alpaca import AlpacaBroker
from ml4t.live.orders import (
    BrokerOrderContractError,
    OrderValidationError,
    UnsupportedOrderCapabilityError,
)
from ml4t.live.safety import (
    LiveRiskConfig,
    OrderReplacementGapError,
    RiskLimitError,
    SafeBroker,
)

ALL_CAPABILITIES = frozenset(
    {
        ExecutionCapability.LIMIT,
        ExecutionCapability.STOP,
        ExecutionCapability.STOP_LIMIT,
        ExecutionCapability.CLOSE_AUCTION,
        ExecutionCapability.PARTIAL_FILL,
    }
)


class RecordingBroker:
    def __init__(self, *, capabilities=ALL_CAPABILITIES) -> None:
        self._connected = False
        self.execution_capabilities = capabilities
        self._positions: dict[str, Position] = {}
        self._pending_orders: list[Order] = []
        self.submit_calls: list[dict[str, Any]] = []
        self.cancel_calls: list[str] = []
        self.close_calls: list[str] = []
        self.submit_error: Exception | None = None
        self.result_override: Order | None = None

    @property
    def positions(self) -> dict[str, Position]:
        return dict(self._positions)

    @property
    def pending_orders(self) -> list[Order]:
        return list(self._pending_orders)

    def get_position(self, asset: str) -> Position | None:
        return self._positions.get(asset.upper())

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
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
        self.submit_calls.append(
            {
                "asset": asset,
                "quantity": quantity,
                "side": side,
                "order_type": order_type,
                "limit_price": limit_price,
                "stop_price": stop_price,
                "kwargs": kwargs,
            }
        )
        if self.submit_error is not None:
            raise self.submit_error
        order = self.result_override or Order(
            asset=asset,
            quantity=quantity,
            side=side or OrderSide.BUY,
            order_type=order_type,
            limit_price=limit_price,
            stop_price=stop_price,
            order_id=f"recording-{len(self.submit_calls)}",
            status=OrderStatus.PENDING,
            created_at=datetime.now(UTC),
        )
        if order.status is OrderStatus.PENDING:
            self._pending_orders.append(order)
        return order

    async def cancel_order_async(self, order_id: str) -> bool:
        self.cancel_calls.append(order_id)
        return any(order.order_id == order_id for order in self._pending_orders)

    async def replace_order_async(self, order_id: str, **kwargs: Any) -> Order:
        raise NotImplementedError

    async def close_position_async(self, asset: str) -> Order | None:
        self.close_calls.append(asset)
        position = self.get_position(asset)
        if position is None:
            return None
        side = OrderSide.SELL if position.quantity > 0 else OrderSide.BUY
        return Order(
            asset=asset.upper(),
            quantity=abs(position.quantity),
            side=side,
            order_id=f"close-{len(self.close_calls)}",
            status=OrderStatus.PENDING,
            created_at=datetime.now(UTC),
        )


def risk_config(tmp_path, **overrides: Any) -> LiveRiskConfig:
    values = {
        "state_file": str(tmp_path / "state.json"),
        "journal_file": str(tmp_path / "journal.jsonl"),
        "max_position_value": 1_000_000.0,
        "max_total_exposure": 1_000_000.0,
        "max_order_value": 1_000_000.0,
        "max_position_shares": 1_000_000,
        "max_order_shares": 1_000_000,
    }
    values.update(overrides)
    return LiveRiskConfig(**values)


def safe_broker(tmp_path, broker: RecordingBroker | None = None, **config) -> SafeBroker:
    broker = broker or RecordingBroker()
    safe = SafeBroker(broker, risk_config(tmp_path, **config))
    safe.record_market_snapshot("SPY", 100.0)
    safe.record_market_snapshot("MSFT", 200.0)
    return safe


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("quantity", "side", "expected_side", "expected_quantity"),
    [
        (2, None, OrderSide.BUY, 2.0),
        (-2, None, OrderSide.SELL, 2.0),
        (2.5, OrderSide.BUY, OrderSide.BUY, 2.5),
        (2.5, OrderSide.SELL, OrderSide.SELL, 2.5),
    ],
)
async def test_accepted_request_is_identical_at_risk_and_adapter_boundaries(
    tmp_path,
    quantity,
    side,
    expected_side,
    expected_quantity,
) -> None:
    broker = RecordingBroker()
    safe = safe_broker(tmp_path, broker)

    order = await safe.submit_order_async(" spy ", quantity, side=side)

    assert broker.submit_calls == [
        {
            "asset": "SPY",
            "quantity": expected_quantity,
            "side": expected_side,
            "order_type": OrderType.MARKET,
            "limit_price": None,
            "stop_price": None,
            "kwargs": {},
        }
    ]
    assert (order.asset, order.side, order.quantity) == (
        "SPY",
        expected_side,
        expected_quantity,
    )
    assert safe._state.orders_placed == 1
    assert len(safe._order_timestamps) == 1
    assert safe._recent_orders[0][1:] == ("SPY", expected_quantity)


@pytest.mark.asyncio
async def test_real_alpaca_boundary_rejects_negative_quantity_with_explicit_side(tmp_path) -> None:
    broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
    client = MagicMock()
    broker._trading_client = client
    broker._connected = True
    broker._account_id = "PA-TEST"
    safe = SafeBroker(broker, risk_config(tmp_path))
    safe.record_market_snapshot("SPY", 100.0)

    with pytest.raises(OrderValidationError, match="positive and unsigned"):
        await safe.submit_order_async("SPY", -10, side=OrderSide.BUY)

    client.submit_order.assert_not_called()


@pytest.mark.asyncio
async def test_real_alpaca_boundary_submits_inferred_fractional_sell_unchanged(tmp_path) -> None:
    broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
    client = MagicMock()
    client.get_account.return_value = SimpleNamespace(
        account_number="PA-TEST",
        equity="100000",
        cash="50000",
    )
    client.submit_order.return_value = SimpleNamespace(
        id="venue-1",
        status=AlpacaOrderStatus.NEW,
        created_at=datetime.now(UTC),
    )
    broker._trading_client = client
    broker._connected = True
    broker._account_id = "PA-TEST"
    safe = SafeBroker(broker, risk_config(tmp_path))
    safe.record_market_snapshot("SPY", 100.0)

    order = await safe.submit_order_async("spy", -2.5)
    venue_request = client.submit_order.call_args.args[0]

    assert order.side is OrderSide.SELL
    assert order.quantity == 2.5
    assert venue_request.side.value == "sell"
    assert float(venue_request.qty) == 2.5


INVALID_REQUESTS = [
    {"quantity": -1, "side": OrderSide.BUY},
    {"quantity": 0},
    {"quantity": float("nan")},
    {"quantity": float("inf")},
    {"quantity": float("-inf")},
    {"quantity": 1, "order_type": OrderType.LIMIT},
    {"quantity": 1, "order_type": OrderType.STOP},
    {"quantity": 1, "order_type": OrderType.MARKET, "limit_price": 100},
    {"quantity": 1, "order_type": OrderType.TRAILING_STOP},
]


@pytest.mark.asyncio
@pytest.mark.parametrize("order_input", INVALID_REQUESTS)
async def test_invalid_request_is_atomic_before_adapter_or_persistence(
    tmp_path, order_input
) -> None:
    broker = RecordingBroker()
    safe = safe_broker(tmp_path, broker)
    before = {
        "state": deepcopy(safe._state.to_dict()),
        "rate": list(safe._order_timestamps),
        "recent": list(safe._recent_orders),
        "portable": safe.load_portable_strategy_state(),
    }

    with pytest.raises((OrderValidationError, UnsupportedOrderCapabilityError)):
        await safe.submit_order_async("SPY", **order_input)

    assert broker.submit_calls == []
    assert safe._state.to_dict() == before["state"]
    assert safe._order_timestamps == before["rate"]
    assert safe._recent_orders == before["recent"]
    assert safe.load_portable_strategy_state() == before["portable"]
    assert not (tmp_path / "state.json").exists()
    assert not (tmp_path / "journal.jsonl").exists()


@pytest.mark.asyncio
async def test_missing_capability_rejects_before_adapter_call(tmp_path) -> None:
    broker = RecordingBroker(capabilities=frozenset())
    safe = safe_broker(tmp_path, broker)

    with pytest.raises(UnsupportedOrderCapabilityError, match="limit"):
        await safe.submit_order_async(
            "SPY",
            1,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            limit_price=100,
        )

    assert broker.submit_calls == []


@pytest.mark.asyncio
async def test_repeated_risk_rejections_do_not_consume_accepted_rate_capacity(tmp_path) -> None:
    broker = RecordingBroker()
    safe = safe_broker(
        tmp_path,
        broker,
        max_order_shares=1,
        max_orders_per_minute=1,
    )
    before = deepcopy(safe._state.to_dict())

    for _ in range(20):
        with pytest.raises(RiskLimitError, match="quantity"):
            await safe.submit_order_async("SPY", 2)

    assert broker.submit_calls == []
    assert safe._order_timestamps == []
    assert safe._recent_orders == []
    assert safe._state.to_dict() == before
    await safe.submit_order_async("SPY", 1)
    with pytest.raises(RiskLimitError, match="Rate limit"):
        await safe.submit_order_async("MSFT", 1)
    assert len(broker.submit_calls) == 1


@pytest.mark.asyncio
async def test_venue_rejection_does_not_commit_accepted_order_state(tmp_path) -> None:
    broker = RecordingBroker()
    broker.result_override = Order(
        asset="SPY",
        quantity=1,
        side=OrderSide.BUY,
        order_id="rejected-1",
        status=OrderStatus.REJECTED,
        rejection_reason="venue rejected",
        created_at=datetime.now(UTC),
    )
    safe = safe_broker(tmp_path, broker)
    before = deepcopy(safe._state.to_dict())

    order = await safe.submit_order_async("SPY", 1)

    assert order.status is OrderStatus.REJECTED
    assert safe._state.to_dict() == before
    assert safe._order_timestamps == []
    assert safe._recent_orders == []


@pytest.mark.asyncio
async def test_adapter_result_mismatch_does_not_commit_accepted_order_state(tmp_path) -> None:
    broker = RecordingBroker()
    broker.result_override = Order(
        asset="SPY",
        quantity=1,
        side=OrderSide.SELL,
        order_id="wrong-1",
        status=OrderStatus.PENDING,
        created_at=datetime.now(UTC),
    )
    safe = safe_broker(tmp_path, broker)
    before = deepcopy(safe._state.to_dict())

    with pytest.raises(BrokerOrderContractError, match="differs"):
        await safe.submit_order_async("SPY", 1, side=OrderSide.BUY)

    assert len(broker.submit_calls) == 1
    assert safe._state.to_dict() == before
    assert safe._order_timestamps == []
    assert safe._recent_orders == []


@pytest.mark.asyncio
async def test_reducing_risk_is_validated_but_exempt_from_entry_rate_limits(tmp_path) -> None:
    broker = RecordingBroker()
    broker._positions["SPY"] = Position("SPY", 10, 100, datetime.now(UTC))
    safe = safe_broker(tmp_path, broker, max_orders_per_minute=1)
    safe._order_timestamps = [datetime.now(UTC).timestamp()]

    with pytest.raises(OrderValidationError, match="finite"):
        await safe.reduce_position_async(
            "SPY",
            float("nan"),
            reason="risk",
            idempotency_key="reduce-invalid",
        )
    assert broker.submit_calls == []
    assert broker.close_calls == []

    order = await safe.close_position_async("SPY")
    assert order is not None
    assert order.side is OrderSide.SELL
    assert broker.close_calls == ["SPY"]
    assert safe._state.orders_placed == 1
    assert len(safe._order_timestamps) == 1


def pending_limit(order_id: str, limit_price: float) -> Order:
    return Order(
        asset="SPY",
        quantity=10,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        limit_price=limit_price,
        order_id=order_id,
        status=OrderStatus.PENDING,
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_failed_cancel_and_resubmit_gap_is_persisted_and_visible(tmp_path) -> None:
    broker = RecordingBroker()
    original = pending_limit("original-1", 100)
    broker._pending_orders = [original]
    broker.submit_error = RuntimeError("venue unavailable")
    safe = safe_broker(tmp_path, broker)

    with pytest.raises(OrderReplacementGapError, match="cancellation was requested"):
        await safe.replace_order_async("original-1", limit_price=99)

    assert safe.replacement_gaps["original-1"]["status"] == "replacement_failed"
    safe.close_persistence()
    restored = SafeBroker(broker, risk_config(tmp_path))
    assert restored.replacement_gaps["original-1"]["status"] == "replacement_failed"
    report = await restored.preview_reconciliation_async()
    assert report["clean"] is False
    assert "original-1" in report["unresolved_replacement_gaps"]


@pytest.mark.asyncio
async def test_cancel_and_resubmit_gap_reconciles_only_after_original_disappears(tmp_path) -> None:
    broker = RecordingBroker()
    original = pending_limit("original-1", 100)
    broker._pending_orders = [original]
    safe = safe_broker(tmp_path, broker, fail_on_reconciliation_mismatch=True)

    replacement = await safe.replace_order_async("original-1", limit_price=99)
    assert replacement.order_id == "recording-1"
    assert safe.replacement_gaps["original-1"]["status"] == "replacement_submitted"
    unresolved = await safe.preview_reconciliation_async()
    assert unresolved["clean"] is False

    broker._pending_orders = [replacement]
    resolved = await safe.preview_reconciliation_async()
    assert resolved["clean"] is True
    assert "original-1" in resolved["resolved_replacement_gaps"]

    await safe.connect()
    assert safe.replacement_gaps == {}
    safe.close_persistence()
    restored = SafeBroker(broker, safe.config)
    assert restored.replacement_gaps == {}
