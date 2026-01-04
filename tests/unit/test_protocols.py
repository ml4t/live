"""Unit tests for protocol definitions.

Tests verify that protocols are runtime_checkable and that mock implementations
satisfy the protocol interfaces.
"""

import pytest
from typing import Any, AsyncIterator
from datetime import datetime

from ml4t.live.protocols import (
    BrokerProtocol,
    AsyncBrokerProtocol,
    DataFeedProtocol,
)
from ml4t.backtest.types import Order, Position, OrderType, OrderSide, OrderStatus


# === Mock Implementations ===


class MockBroker:
    """Mock implementation of BrokerProtocol for testing."""

    def __init__(self):
        self._positions: dict[str, Position] = {}
        self._pending_orders: list[Order] = []
        self._connected = True
        self._cash = 100000.0

    @property
    def positions(self) -> dict[str, Position]:
        return self._positions.copy()

    @property
    def pending_orders(self) -> list[Order]:
        return self._pending_orders.copy()

    @property
    def is_connected(self) -> bool:
        return self._connected

    def get_position(self, asset: str) -> Position | None:
        return self._positions.get(asset)

    def get_account_value(self) -> float:
        position_value = sum(p.market_value for p in self._positions.values())
        return self._cash + position_value

    def get_cash(self) -> float:
        return self._cash

    def submit_order(
        self,
        asset: str,
        quantity: int,
        side: OrderSide | None = None,
        order_type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
        stop_price: float | None = None,
        **kwargs: Any,
    ) -> Order:
        if side is None:
            side = OrderSide.BUY if quantity > 0 else OrderSide.SELL

        order = Order(
            asset=asset,
            side=side,
            quantity=abs(quantity),
            order_type=order_type,
            limit_price=limit_price,
            stop_price=stop_price,
            order_id="order-1",
            status=OrderStatus.PENDING,
        )
        self._pending_orders.append(order)
        return order

    def cancel_order(self, order_id: str) -> bool:
        for order in self._pending_orders:
            if order.order_id == order_id:
                self._pending_orders.remove(order)
                return True
        return False

    def close_position(self, asset: str) -> Order | None:
        pos = self.get_position(asset)
        if pos is None:
            return None
        # Submit closing order
        quantity = -pos.quantity if pos.quantity > 0 else abs(pos.quantity)
        return self.submit_order(asset, quantity)


class MockAsyncBroker:
    """Mock implementation of AsyncBrokerProtocol for testing."""

    def __init__(self):
        self._positions: dict[str, Position] = {}
        self._pending_orders: list[Order] = []
        self._connected = False
        self._cash = 100000.0

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def is_connected_async(self) -> bool:
        return self._connected

    async def get_positions_async(self) -> dict[str, Position]:
        return self._positions.copy()

    async def get_pending_orders_async(self) -> list[Order]:
        return self._pending_orders.copy()

    async def get_position_async(self, asset: str) -> Position | None:
        return self._positions.get(asset)

    async def get_account_value_async(self) -> float:
        position_value = sum(p.market_value for p in self._positions.values())
        return self._cash + position_value

    async def get_cash_async(self) -> float:
        return self._cash

    async def submit_order_async(
        self,
        asset: str,
        quantity: int,
        side: OrderSide | None = None,
        order_type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
        stop_price: float | None = None,
        **kwargs: Any,
    ) -> Order:
        if side is None:
            side = OrderSide.BUY if quantity > 0 else OrderSide.SELL

        order = Order(
            asset=asset,
            side=side,
            quantity=abs(quantity),
            order_type=order_type,
            limit_price=limit_price,
            stop_price=stop_price,
            order_id="order-1",
            status=OrderStatus.PENDING,
        )
        self._pending_orders.append(order)
        return order

    async def cancel_order_async(self, order_id: str) -> bool:
        for order in self._pending_orders:
            if order.order_id == order_id:
                self._pending_orders.remove(order)
                return True
        return False

    async def close_position_async(self, asset: str) -> Order | None:
        pos = self._positions.get(asset)
        if pos is None:
            return None
        quantity = -pos.quantity if pos.quantity > 0 else abs(pos.quantity)
        return await self.submit_order_async(asset, quantity)


class MockDataFeed:
    """Mock implementation of DataFeedProtocol for testing."""

    def __init__(self, data_points: list[tuple[datetime, dict[str, dict], dict]]):
        self._data_points = data_points
        self._index = 0
        self._started = False

    async def start(self) -> None:
        self._started = True

    def stop(self) -> None:
        self._started = False

    def __aiter__(self) -> AsyncIterator[tuple[datetime, dict[str, dict], dict]]:
        return self

    async def __anext__(self) -> tuple[datetime, dict[str, dict], dict]:
        if not self._started or self._index >= len(self._data_points):
            raise StopAsyncIteration
        data = self._data_points[self._index]
        self._index += 1
        return data


# === Test Protocol Compliance ===


def test_broker_protocol_is_runtime_checkable():
    """Test that BrokerProtocol is runtime_checkable."""
    broker = MockBroker()
    assert isinstance(broker, BrokerProtocol), "MockBroker should satisfy BrokerProtocol"


def test_async_broker_protocol_is_runtime_checkable():
    """Test that AsyncBrokerProtocol is runtime_checkable."""
    broker = MockAsyncBroker()
    assert isinstance(
        broker, AsyncBrokerProtocol
    ), "MockAsyncBroker should satisfy AsyncBrokerProtocol"


def test_data_feed_protocol_is_runtime_checkable():
    """Test that DataFeedProtocol is runtime_checkable."""
    feed = MockDataFeed([])
    assert isinstance(
        feed, DataFeedProtocol
    ), "MockDataFeed should satisfy DataFeedProtocol"


# === Test Mock Implementations ===


def test_mock_broker_basic_operations():
    """Test basic operations on MockBroker."""
    broker = MockBroker()

    # Initial state
    assert broker.is_connected
    assert broker.get_cash() == 100000.0
    assert len(broker.positions) == 0
    assert len(broker.pending_orders) == 0

    # Submit order
    order = broker.submit_order("AAPL", 100)
    assert order.asset == "AAPL"
    assert order.quantity == 100
    assert order.side == OrderSide.BUY
    assert order.status == OrderStatus.PENDING

    # Check pending orders
    assert len(broker.pending_orders) == 1

    # Cancel order
    assert broker.cancel_order(order.order_id)
    assert len(broker.pending_orders) == 0

    # Cancel nonexistent order
    assert not broker.cancel_order("nonexistent")


def test_mock_broker_position_operations():
    """Test position-related operations on MockBroker."""
    broker = MockBroker()

    # No position initially
    assert broker.get_position("AAPL") is None

    # Add a position
    pos = Position(
        asset="AAPL",
        quantity=100,
        entry_price=150.0,
        entry_time=datetime.now(),
        current_price=155.0,
    )
    broker._positions["AAPL"] = pos

    # Get position
    retrieved_pos = broker.get_position("AAPL")
    assert retrieved_pos is not None
    assert retrieved_pos.asset == "AAPL"
    assert retrieved_pos.quantity == 100

    # Close position
    closing_order = broker.close_position("AAPL")
    assert closing_order is not None
    assert closing_order.asset == "AAPL"
    assert closing_order.side == OrderSide.SELL
    assert closing_order.quantity == 100

    # Close nonexistent position
    assert broker.close_position("TSLA") is None


def test_mock_broker_order_side_detection():
    """Test automatic order side detection from quantity."""
    broker = MockBroker()

    # Positive quantity → BUY
    buy_order = broker.submit_order("AAPL", 100)
    assert buy_order.side == OrderSide.BUY
    assert buy_order.quantity == 100

    # Negative quantity → SELL
    sell_order = broker.submit_order("AAPL", -50)
    assert sell_order.side == OrderSide.SELL
    assert sell_order.quantity == 50

    # Explicit side overrides
    explicit_buy = broker.submit_order("AAPL", -100, side=OrderSide.BUY)
    assert explicit_buy.side == OrderSide.BUY


@pytest.mark.asyncio
async def test_mock_async_broker_basic_operations():
    """Test basic operations on MockAsyncBroker."""
    broker = MockAsyncBroker()

    # Not connected initially
    assert not await broker.is_connected_async()

    # Connect
    await broker.connect()
    assert await broker.is_connected_async()

    # Get cash
    cash = await broker.get_cash_async()
    assert cash == 100000.0

    # Submit order
    order = await broker.submit_order_async("AAPL", 100)
    assert order.asset == "AAPL"
    assert order.side == OrderSide.BUY

    # Get pending orders
    orders = await broker.get_pending_orders_async()
    assert len(orders) == 1

    # Cancel order
    assert await broker.cancel_order_async(order.order_id)
    orders = await broker.get_pending_orders_async()
    assert len(orders) == 0

    # Disconnect
    await broker.disconnect()
    assert not await broker.is_connected_async()


@pytest.mark.asyncio
async def test_mock_async_broker_position_operations():
    """Test position operations on MockAsyncBroker."""
    broker = MockAsyncBroker()
    await broker.connect()

    # No position initially
    pos = await broker.get_position_async("AAPL")
    assert pos is None

    # Add position
    broker._positions["AAPL"] = Position(
        asset="AAPL",
        quantity=100,
        entry_price=150.0,
        entry_time=datetime.now(),
        current_price=155.0,
    )

    # Get position
    pos = await broker.get_position_async("AAPL")
    assert pos is not None
    assert pos.quantity == 100

    # Get all positions
    positions = await broker.get_positions_async()
    assert "AAPL" in positions

    # Close position
    closing_order = await broker.close_position_async("AAPL")
    assert closing_order is not None
    assert closing_order.side == OrderSide.SELL


@pytest.mark.asyncio
async def test_mock_data_feed_iteration():
    """Test async iteration over MockDataFeed."""
    data_points = [
        (
            datetime(2023, 1, 1, 9, 30),
            {"AAPL": {"open": 150, "high": 151, "low": 149, "close": 150.5, "volume": 1000}},
            {},
        ),
        (
            datetime(2023, 1, 1, 9, 31),
            {"AAPL": {"open": 150.5, "high": 152, "low": 150, "close": 151.5, "volume": 1200}},
            {},
        ),
    ]

    feed = MockDataFeed(data_points)
    await feed.start()

    # Iterate through data
    collected = []
    async for timestamp, data, context in feed:
        collected.append((timestamp, data, context))

    assert len(collected) == 2
    assert collected[0][0] == datetime(2023, 1, 1, 9, 30)
    assert "AAPL" in collected[0][1]


@pytest.mark.asyncio
async def test_mock_data_feed_stop():
    """Test stopping a data feed."""
    feed = MockDataFeed([
        (datetime.now(), {"AAPL": {"close": 150}}, {})
    ])
    await feed.start()
    assert feed._started

    feed.stop()
    assert not feed._started

    # Iteration should stop
    with pytest.raises(StopAsyncIteration):
        await feed.__anext__()


# === Test Protocol Method Signatures ===


def test_broker_protocol_has_all_required_methods():
    """Verify BrokerProtocol has all required method signatures."""
    required_attrs = [
        "positions",
        "pending_orders",
        "is_connected",
        "get_position",
        "get_account_value",
        "get_cash",
        "submit_order",
        "cancel_order",
        "close_position",
    ]

    broker = MockBroker()
    for attr in required_attrs:
        assert hasattr(broker, attr), f"BrokerProtocol missing: {attr}"


def test_async_broker_protocol_has_all_required_methods():
    """Verify AsyncBrokerProtocol has all required method signatures."""
    required_attrs = [
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
        "close_position_async",
    ]

    broker = MockAsyncBroker()
    for attr in required_attrs:
        assert hasattr(broker, attr), f"AsyncBrokerProtocol missing: {attr}"


def test_data_feed_protocol_has_all_required_methods():
    """Verify DataFeedProtocol has all required method signatures."""
    required_attrs = ["start", "stop", "__aiter__", "__anext__"]

    feed = MockDataFeed([])
    for attr in required_attrs:
        assert hasattr(feed, attr), f"DataFeedProtocol missing: {attr}"
