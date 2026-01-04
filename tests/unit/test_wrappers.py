"""Unit tests for ThreadSafeBrokerWrapper.

Tests cover:
- Sync/async bridging via run_coroutine_threadsafe
- Property access (positions, pending_orders, is_connected)
- Order submission with correct timeout
- Cancel and close operations
- Timeout handling (5s getters, 30s orders)
- Error handling

IMPORTANT: Tests must call wrapper methods from worker thread
(via asyncio.to_thread) to simulate real usage pattern.
"""

import pytest
import asyncio
from datetime import datetime

from ml4t.live.wrappers import ThreadSafeBrokerWrapper
from ml4t.backtest.types import Order, Position, OrderSide, OrderType, OrderStatus


# === Mock Async Broker ===


class MockAsyncBroker:
    """Mock implementation of AsyncBrokerProtocol for testing."""

    def __init__(self):
        self._positions: dict[str, Position] = {}
        self._pending_orders: list[Order] = []
        self._connected = True
        self._cash = 100_000.0
        self._account_value = 100_000.0
        self._delay = 0.0

    @property
    def positions(self) -> dict[str, Position]:
        return self._positions.copy()

    @property
    def pending_orders(self) -> list[Order]:
        return self._pending_orders.copy()

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def is_connected_async(self) -> bool:
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        return self._connected

    async def get_positions_async(self) -> dict[str, Position]:
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        return self._positions.copy()

    async def get_pending_orders_async(self) -> list[Order]:
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        return self._pending_orders.copy()

    async def get_position_async(self, asset: str) -> Position | None:
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        return self._positions.get(asset)

    async def get_account_value_async(self) -> float:
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        return self._account_value

    async def get_cash_async(self) -> float:
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        return self._cash

    async def submit_order_async(
        self,
        asset: str,
        quantity: int,
        side: OrderSide | None = None,
        order_type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
        stop_price: float | None = None,
        **kwargs,
    ) -> Order:
        if self._delay > 0:
            await asyncio.sleep(self._delay)

        if side is None:
            side = OrderSide.BUY if quantity > 0 else OrderSide.SELL

        order = Order(
            asset=asset,
            side=side,
            quantity=abs(quantity),
            order_type=order_type,
            limit_price=limit_price,
            stop_price=stop_price,
            order_id=f"order-{asset}-{len(self._pending_orders)}",
            status=OrderStatus.PENDING,
        )
        self._pending_orders.append(order)
        return order

    async def cancel_order_async(self, order_id: str) -> bool:
        if self._delay > 0:
            await asyncio.sleep(self._delay)

        for order in self._pending_orders:
            if order.order_id == order_id:
                self._pending_orders.remove(order)
                return True
        return False

    async def close_position_async(self, asset: str) -> Order | None:
        if self._delay > 0:
            await asyncio.sleep(self._delay)

        pos = self._positions.get(asset)
        if pos is None:
            return None

        quantity = -pos.quantity if pos.quantity > 0 else abs(pos.quantity)
        return await self.submit_order_async(asset, int(quantity))


# === Tests ===


@pytest.mark.asyncio
async def test_wrapper_initialization():
    """Test ThreadSafeBrokerWrapper initialization."""
    broker = MockAsyncBroker()
    loop = asyncio.get_running_loop()
    wrapper = ThreadSafeBrokerWrapper(broker, loop)

    assert wrapper._broker is broker
    assert wrapper._loop is loop


@pytest.mark.asyncio
async def test_positions_property():
    """Test positions property access (no async needed)."""
    broker = MockAsyncBroker()
    broker._positions["AAPL"] = Position(
        asset="AAPL",
        quantity=100,
        entry_price=150.0,
        entry_time=datetime.now(),
        current_price=155.0,
    )

    loop = asyncio.get_running_loop()
    wrapper = ThreadSafeBrokerWrapper(broker, loop)

    positions = wrapper.positions
    assert "AAPL" in positions
    assert positions["AAPL"].quantity == 100


@pytest.mark.asyncio
async def test_pending_orders_property():
    """Test pending_orders property access (no async needed)."""
    broker = MockAsyncBroker()
    order = Order(
        asset="AAPL",
        side=OrderSide.BUY,
        quantity=100,
        order_id="test-1",
        status=OrderStatus.PENDING,
    )
    broker._pending_orders.append(order)

    loop = asyncio.get_running_loop()
    wrapper = ThreadSafeBrokerWrapper(broker, loop)

    orders = wrapper.pending_orders
    assert len(orders) == 1
    assert orders[0].order_id == "test-1"


@pytest.mark.asyncio
async def test_is_connected_property():
    """Test is_connected property."""
    broker = MockAsyncBroker()
    loop = asyncio.get_running_loop()
    wrapper = ThreadSafeBrokerWrapper(broker, loop)

    def worker():
        assert wrapper.is_connected is True
        broker._connected = False
        assert wrapper.is_connected is False

    await asyncio.to_thread(worker)


@pytest.mark.asyncio
async def test_get_position():
    """Test get_position method."""
    broker = MockAsyncBroker()
    broker._positions["AAPL"] = Position(
        asset="AAPL",
        quantity=100,
        entry_price=150.0,
        entry_time=datetime.now(),
        current_price=155.0,
    )

    loop = asyncio.get_running_loop()
    wrapper = ThreadSafeBrokerWrapper(broker, loop)

    # get_position uses .positions property, so no async needed
    pos = wrapper.get_position("AAPL")
    assert pos is not None
    assert pos.asset == "AAPL"
    assert pos.quantity == 100
    assert wrapper.get_position("TSLA") is None


@pytest.mark.asyncio
async def test_get_cash():
    """Test get_cash method with 5s timeout."""
    broker = MockAsyncBroker()
    broker._cash = 75_000.0

    loop = asyncio.get_running_loop()
    wrapper = ThreadSafeBrokerWrapper(broker, loop)

    def worker():
        return wrapper.get_cash()

    cash = await asyncio.to_thread(worker)
    assert cash == 75_000.0


@pytest.mark.asyncio
async def test_get_account_value():
    """Test get_account_value method with 5s timeout."""
    broker = MockAsyncBroker()
    broker._account_value = 125_000.0

    loop = asyncio.get_running_loop()
    wrapper = ThreadSafeBrokerWrapper(broker, loop)

    def worker():
        return wrapper.get_account_value()

    value = await asyncio.to_thread(worker)
    assert value == 125_000.0


@pytest.mark.asyncio
async def test_submit_order():
    """Test submit_order method with 30s timeout."""
    broker = MockAsyncBroker()
    loop = asyncio.get_running_loop()
    wrapper = ThreadSafeBrokerWrapper(broker, loop)

    def worker():
        return wrapper.submit_order("AAPL", 100, OrderSide.BUY)

    order = await asyncio.to_thread(worker)
    assert order is not None
    assert order.asset == "AAPL"
    assert order.side == OrderSide.BUY
    assert order.quantity == 100
    assert order.status == OrderStatus.PENDING


@pytest.mark.asyncio
async def test_submit_order_auto_side_detection():
    """Test that order side is auto-detected from quantity."""
    broker = MockAsyncBroker()
    loop = asyncio.get_running_loop()
    wrapper = ThreadSafeBrokerWrapper(broker, loop)

    def worker():
        buy_order = wrapper.submit_order("AAPL", 100)
        sell_order = wrapper.submit_order("AAPL", -50)
        return buy_order, sell_order

    buy_order, sell_order = await asyncio.to_thread(worker)
    assert buy_order.side == OrderSide.BUY
    assert sell_order.side == OrderSide.SELL


@pytest.mark.asyncio
async def test_cancel_order():
    """Test cancel_order method with 30s timeout."""
    broker = MockAsyncBroker()
    loop = asyncio.get_running_loop()
    wrapper = ThreadSafeBrokerWrapper(broker, loop)

    def worker():
        order = wrapper.submit_order("AAPL", 100)
        order_id = order.order_id
        result1 = wrapper.cancel_order(order_id)
        result2 = wrapper.cancel_order(order_id)  # Second time should fail
        return result1, result2

    result1, result2 = await asyncio.to_thread(worker)
    assert result1 is True
    assert result2 is False


@pytest.mark.asyncio
async def test_close_position():
    """Test close_position method with 30s timeout."""
    broker = MockAsyncBroker()
    broker._positions["AAPL"] = Position(
        asset="AAPL",
        quantity=100,
        entry_price=150.0,
        entry_time=datetime.now(),
        current_price=155.0,
    )

    loop = asyncio.get_running_loop()
    wrapper = ThreadSafeBrokerWrapper(broker, loop)

    def worker():
        return wrapper.close_position("AAPL")

    order = await asyncio.to_thread(worker)
    assert order is not None
    assert order.asset == "AAPL"
    assert order.side == OrderSide.SELL
    assert order.quantity == 100


@pytest.mark.asyncio
async def test_close_position_nonexistent():
    """Test close_position with nonexistent position."""
    broker = MockAsyncBroker()
    loop = asyncio.get_running_loop()
    wrapper = ThreadSafeBrokerWrapper(broker, loop)

    def worker():
        return wrapper.close_position("TSLA")

    order = await asyncio.to_thread(worker)
    assert order is None


@pytest.mark.asyncio
async def test_timeout_on_slow_operation():
    """Test that slow operations timeout correctly."""
    broker = MockAsyncBroker()
    broker._delay = 10.0  # 10 second delay

    loop = asyncio.get_running_loop()
    wrapper = ThreadSafeBrokerWrapper(broker, loop)

    def worker():
        wrapper.get_cash()  # Should timeout after 5s

    with pytest.raises(TimeoutError):
        await asyncio.to_thread(worker)


@pytest.mark.asyncio
async def test_timeout_differentiation():
    """Test that getters have 5s timeout and orders have 30s timeout."""
    broker = MockAsyncBroker()
    loop = asyncio.get_running_loop()
    wrapper = ThreadSafeBrokerWrapper(broker, loop)

    # Test: 6s delay should timeout getters but not orders
    broker._delay = 6.0

    def worker_cash():
        wrapper.get_cash()  # Should timeout

    with pytest.raises(TimeoutError):
        await asyncio.to_thread(worker_cash)

    # Orders should NOT timeout with 6s delay (30s timeout)
    def worker_order():
        return wrapper.submit_order("AAPL", 100)

    order = await asyncio.to_thread(worker_order)
    assert order is not None


@pytest.mark.asyncio
async def test_run_from_worker_thread():
    """Test comprehensive strategy-like usage from worker thread."""
    broker = MockAsyncBroker()
    loop = asyncio.get_running_loop()
    wrapper = ThreadSafeBrokerWrapper(broker, loop)

    def strategy_code():
        # Typical strategy operations
        cash = wrapper.get_cash()
        assert cash == 100_000.0

        order = wrapper.submit_order("AAPL", 100)
        assert order.asset == "AAPL"

        positions = wrapper.positions
        assert isinstance(positions, dict)

        return True

    result = await asyncio.to_thread(strategy_code)
    assert result is True
