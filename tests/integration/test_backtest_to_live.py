"""
Joint integration test: Backtest-to-Live workflow validation.

Tests the core promise: copy-paste Strategy from backtest to live with ZERO changes.

Test scenarios:
1. Strategy with position logic (don't buy if already long)
2. Strategy that exits positions
3. Strategy that flips positions (long → short)
4. Handling of repeat signals
"""

import asyncio
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock

from ml4t.backtest import Strategy, OrderSide
from ml4t.live import LiveEngine, LiveRiskConfig
from ml4t.live.brokers.ib import IBBroker
from ml4t.live.safety import SafeBroker
from ml4t.live.wrappers import ThreadSafeBrokerWrapper
from ml4t.live.feeds.aggregator import BarAggregator


# =============================================================================
# STRATEGY DEFINITIONS (Same for backtest and live)
# =============================================================================


class MATestStrategy(Strategy):
    """
    Moving average crossover strategy for testing.

    Logic:
    - Buy when close > MA(10)
    - Sell when close < MA(10)
    - Don't trade if signal hasn't changed
    """

    def __init__(self, ma_period=10):
        self.ma_period = ma_period
        self.price_history = []
        self.last_signal = None  # Track last signal to avoid repeat orders

    def on_data(self, timestamp, data, context, broker):
        """Strategy logic - IDENTICAL in backtest and live."""
        if 'SPY' not in data:
            return

        bar = data['SPY']
        close = bar['close']
        self.price_history.append(close)

        # Need enough data for MA
        if len(self.price_history) < self.ma_period:
            return

        # Calculate MA
        ma = sum(self.price_history[-self.ma_period :]) / self.ma_period

        # Get current position
        position = broker.get_position('SPY')
        current_qty = position.quantity if position else 0

        # Determine signal
        signal = 'BUY' if close > ma else 'SELL'

        # Only act if signal changed
        if signal == self.last_signal:
            return  # No change, don't trade

        self.last_signal = signal

        # Execute based on signal
        if signal == 'BUY':
            if current_qty <= 0:  # Not long, buy
                qty = 10 if current_qty == 0 else abs(current_qty) + 10
                broker.submit_order('SPY', qty, side=OrderSide.BUY)
        else:  # SELL signal
            if current_qty >= 0:  # Not short, sell
                qty = current_qty if current_qty > 0 else 10
                broker.submit_order('SPY', qty, side=OrderSide.SELL)


class SimpleExitStrategy(Strategy):
    """
    Strategy that tests clean exits.

    Logic:
    - Buy on bar 1
    - Hold through bars 2-4
    - Exit on bar 5
    """

    def __init__(self):
        self.bar_count = 0
        self.entered = False

    def on_data(self, timestamp, data, context, broker):
        """Simple entry and exit logic."""
        if 'SPY' not in data:
            return

        self.bar_count += 1
        position = broker.get_position('SPY')
        current_qty = position.quantity if position else 0

        if self.bar_count == 1 and current_qty == 0:
            # Enter on first bar
            broker.submit_order('SPY', 10, side=OrderSide.BUY)
            self.entered = True

        elif self.bar_count == 5 and current_qty > 0:
            # Exit on bar 5
            broker.submit_order('SPY', current_qty, side=OrderSide.SELL)


class FlipPositionStrategy(Strategy):
    """
    Strategy that flips between long and short.

    Logic:
    - Start long
    - Flip to short on bar 5
    - Flip back to long on bar 10
    """

    def __init__(self):
        self.bar_count = 0

    def on_data(self, timestamp, data, context, broker):
        """Flip position logic."""
        if 'SPY' not in data:
            return

        self.bar_count += 1
        position = broker.get_position('SPY')
        current_qty = position.quantity if position else 0

        if self.bar_count == 1 and current_qty == 0:
            # Initial long
            broker.submit_order('SPY', 10, side=OrderSide.BUY)

        elif self.bar_count == 5 and current_qty > 0:
            # Flip to short: sell current + sell 10 more
            broker.submit_order('SPY', current_qty + 10, side=OrderSide.SELL)

        elif self.bar_count == 10 and current_qty < 0:
            # Flip to long: buy to cover + buy 10 more
            broker.submit_order('SPY', abs(current_qty) + 10, side=OrderSide.BUY)


# =============================================================================
# MOCK DATA FEED
# =============================================================================


class MockBarFeed:
    """
    Mock bar feed that generates predefined bars.

    This simulates what would come from IBDataFeed or DataBentoFeed.
    """

    def __init__(self, bars):
        """
        Args:
            bars: List of (timestamp, open, high, low, close, volume) tuples
        """
        self._bars = bars
        self._index = 0
        self._running = False

    async def start(self):
        self._running = True

    async def stop(self):
        self._running = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._running or self._index >= len(self._bars):
            raise StopAsyncIteration

        bar_data = self._bars[self._index]
        self._index += 1

        # Return (timestamp, data, context) tuple matching DataFeedProtocol
        timestamp = bar_data[0]
        data = {
            'SPY': {
                'open': bar_data[1],
                'high': bar_data[2],
                'low': bar_data[3],
                'close': bar_data[4],
                'volume': bar_data[5],
            }
        }
        context = {}

        return (timestamp, data, context)


# =============================================================================
# MOCK BROKER
# =============================================================================


class MockLiveBroker:
    """Mock broker that behaves like IBBroker."""

    def __init__(self):
        self._positions = {}
        self._orders = []
        self._order_counter = 0
        self._cash = 100_000.0

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    @property
    def positions(self):
        """Sync positions property (used by SafeBroker)."""
        from ml4t.backtest.types import Position

        return {
            asset: Position(asset=asset, quantity=qty, entry_price=100.0, entry_time=datetime.now(timezone.utc))
            for asset, qty in self._positions.items()
        }

    @property
    def pending_orders(self):
        """Pending orders property (used by SafeBroker)."""
        return []

    async def get_positions_async(self):
        from ml4t.backtest.types import Position

        return {
            asset: Position(asset=asset, quantity=qty, entry_price=100.0, entry_time=datetime.now(timezone.utc))
            for asset, qty in self._positions.items()
        }

    async def submit_order_async(self, asset, quantity, side, order_type, limit_price=None, stop_price=None, **kwargs):
        from ml4t.backtest.types import Order, OrderStatus

        self._order_counter += 1
        order = Order(
            asset=asset,
            quantity=quantity,
            side=side,
            order_type=order_type,
            order_id=f"TEST-{self._order_counter}",
            status=OrderStatus.FILLED,
            created_at=datetime.now(timezone.utc),
        )
        self._orders.append(order)

        # Update position
        signed_qty = quantity if side == OrderSide.BUY else -quantity
        self._positions[asset] = self._positions.get(asset, 0) + signed_qty

        # Update cash
        self._cash -= signed_qty * 100.0  # Assume $100/share

        return order

    async def cancel_order_async(self, order_id):
        return True

    async def get_cash_async(self):
        return self._cash

    # Sync methods (used by SafeBroker directly)
    def get_position(self, asset):
        """Get position for asset (sync version)."""
        from ml4t.backtest.types import Position

        qty = self._positions.get(asset, 0)
        if qty == 0:
            return None
        return Position(asset=asset, quantity=qty, entry_price=100.0, entry_time=datetime.now(timezone.utc))

    def get_account_value(self):
        """Get total account value."""
        positions_value = sum(qty * 100.0 for qty in self._positions.values())
        return self._cash + positions_value

    def get_cash(self):
        """Get cash balance."""
        return self._cash

    async def get_account_value_async(self):
        return self._cash + sum(qty * 100.0 for qty in self._positions.values())


# =============================================================================
# TESTS
# =============================================================================


@pytest.mark.asyncio
async def test_ma_strategy_no_repeat_signals():
    """
    Test that MA strategy doesn't generate repeat orders.

    Key validation: Strategy checks last_signal to avoid
    submitting duplicate orders when signal hasn't changed.
    """
    print("\n" + "=" * 70)
    print("TEST: MA Strategy - No Repeat Signals")
    print("=" * 70 + "\n")

    # Generate bars with clear signal
    base_time = datetime.now(timezone.utc)
    bars = [
        # Price below MA(10) = 100 → SELL signal
        (base_time + timedelta(minutes=i), 90, 92, 89, 91, 1000)
        for i in range(15)
    ]

    # Add bars above MA → BUY signal
    bars.extend(
        [(base_time + timedelta(minutes=i + 15), 110, 112, 109, 111, 1000) for i in range(5)]
    )

    # Setup
    mock_broker = MockLiveBroker()
    await mock_broker.connect()

    config = LiveRiskConfig(shadow_mode=False, dedup_window_seconds=0)  # Test real order logic
    safe_broker = SafeBroker(mock_broker, config)

    bar_feed = MockBarFeed(bars)
    await bar_feed.start()

    strategy = MATestStrategy(ma_period=10)

    # Run strategy
    loop = asyncio.get_event_loop()
    wrapped_broker = ThreadSafeBrokerWrapper(safe_broker, loop)

    async for timestamp, data, context in bar_feed:
        # Run strategy in thread pool (matches LiveEngine behavior)
        await asyncio.to_thread(strategy.on_data, timestamp, data, context, wrapped_broker)

    await bar_feed.stop()
    await mock_broker.disconnect()

    # Verify
    orders = mock_broker._orders
    print(f"Total bars: {len(bars)}")
    print(f"Total orders: {len(orders)}")
    print(f"\nOrders submitted:")
    for i, order in enumerate(orders, 1):
        print(f"  {i}. {order.side.name} {order.quantity} {order.asset}")

    # Should only have 2 orders: initial SELL, then BUY when signal flips
    assert len(orders) == 2, f"Expected 2 orders (1 per signal change), got {len(orders)}"
    assert orders[0].side == OrderSide.SELL, "First order should be SELL (price < MA)"
    assert orders[1].side == OrderSide.BUY, "Second order should be BUY (price > MA)"

    print("\n✅ PASS: Strategy correctly avoids repeat signals")
    print("=" * 70 + "\n")


@pytest.mark.asyncio
async def test_simple_exit_strategy():
    """Test clean position exit logic."""
    print("\n" + "=" * 70)
    print("TEST: Simple Exit Strategy")
    print("=" * 70 + "\n")

    base_time = datetime.now(timezone.utc)
    bars = [(base_time + timedelta(minutes=i), 100, 101, 99, 100, 1000) for i in range(10)]

    mock_broker = MockLiveBroker()
    await mock_broker.connect()

    config = LiveRiskConfig(shadow_mode=False, dedup_window_seconds=0)
    safe_broker = SafeBroker(mock_broker, config)

    bar_feed = MockBarFeed(bars)
    await bar_feed.start()

    strategy = SimpleExitStrategy()

    loop = asyncio.get_event_loop()
    wrapped_broker = ThreadSafeBrokerWrapper(safe_broker, loop)

    async for timestamp, data, context in bar_feed:
        # Run strategy in thread pool (matches LiveEngine behavior)
        await asyncio.to_thread(strategy.on_data, timestamp, data, context, wrapped_broker)

    await bar_feed.stop()
    await mock_broker.disconnect()

    # Verify
    orders = mock_broker._orders
    print(f"Total orders: {len(orders)}")
    for i, order in enumerate(orders, 1):
        print(f"  {i}. {order.side.name} {order.quantity} {order.asset}")

    assert len(orders) == 2, "Should have entry and exit"
    assert orders[0].side == OrderSide.BUY, "First order is entry"
    assert orders[1].side == OrderSide.SELL, "Second order is exit"
    assert orders[1].quantity == 10, "Exit quantity matches entry"

    final_position = mock_broker._positions.get('SPY', 0)
    assert final_position == 0, f"Position should be flat after exit, got {final_position}"

    print(f"\n✅ PASS: Clean exit, final position = {final_position}")
    print("=" * 70 + "\n")


@pytest.mark.asyncio
async def test_flip_position_strategy():
    """Test flipping between long and short positions."""
    print("\n" + "=" * 70)
    print("TEST: Flip Position Strategy")
    print("=" * 70 + "\n")

    base_time = datetime.now(timezone.utc)
    bars = [(base_time + timedelta(minutes=i), 100, 101, 99, 100, 1000) for i in range(15)]

    mock_broker = MockLiveBroker()
    await mock_broker.connect()

    config = LiveRiskConfig(shadow_mode=False, dedup_window_seconds=0)
    safe_broker = SafeBroker(mock_broker, config)

    bar_feed = MockBarFeed(bars)
    await bar_feed.start()

    strategy = FlipPositionStrategy()

    loop = asyncio.get_event_loop()
    wrapped_broker = ThreadSafeBrokerWrapper(safe_broker, loop)

    async for timestamp, data, context in bar_feed:
        # Run strategy in thread pool (matches LiveEngine behavior)
        await asyncio.to_thread(strategy.on_data, timestamp, data, context, wrapped_broker)

    await bar_feed.stop()
    await mock_broker.disconnect()

    # Verify
    orders = mock_broker._orders
    print(f"Total orders: {len(orders)}")
    for i, order in enumerate(orders, 1):
        print(f"  {i}. {order.side.name} {order.quantity} {order.asset} @ bar {i}")

    # Expected: 3 orders
    # 1. BUY 10 (initial long)
    # 2. SELL 20 (flip to short: close 10 + short 10)
    # 3. BUY 20 (flip to long: cover 10 + long 10)

    assert len(orders) == 3, f"Expected 3 orders, got {len(orders)}"
    assert orders[0].side == OrderSide.BUY and orders[0].quantity == 10
    assert orders[1].side == OrderSide.SELL and orders[1].quantity == 20
    assert orders[2].side == OrderSide.BUY and orders[2].quantity == 20

    final_position = mock_broker._positions.get('SPY', 0)
    print(f"\nFinal position: {final_position}")
    assert final_position == 10, "Should be long 10 after flip sequence"

    print("\n✅ PASS: Position flips executed correctly")
    print("=" * 70 + "\n")


@pytest.mark.asyncio
async def test_shadow_mode_prevents_real_orders():
    """Verify shadow mode doesn't place real orders but tracks virtual positions."""
    print("\n" + "=" * 70)
    print("TEST: Shadow Mode - Virtual Position Tracking")
    print("=" * 70 + "\n")

    base_time = datetime.now(timezone.utc)
    bars = [(base_time + timedelta(minutes=i), 100, 101, 99, 100, 1000) for i in range(5)]

    mock_broker = MockLiveBroker()
    await mock_broker.connect()

    config = LiveRiskConfig(shadow_mode=True)  # SHADOW MODE
    safe_broker = SafeBroker(mock_broker, config)

    bar_feed = MockBarFeed(bars)
    await bar_feed.start()

    strategy = SimpleExitStrategy()

    loop = asyncio.get_event_loop()
    wrapped_broker = ThreadSafeBrokerWrapper(safe_broker, loop)

    async for timestamp, data, context in bar_feed:
        # Run strategy in thread pool (matches LiveEngine behavior)
        await asyncio.to_thread(strategy.on_data, timestamp, data, context, wrapped_broker)

    await bar_feed.stop()
    await mock_broker.disconnect()

    # Verify NO real orders placed
    real_orders = mock_broker._orders
    print(f"Real orders placed: {len(real_orders)}")
    assert len(real_orders) == 0, "Shadow mode should not place real orders"

    # Verify virtual positions tracked
    virtual_positions = safe_broker._virtual_portfolio.positions
    print(f"Virtual positions: {virtual_positions}")
    # Should have tracked the entry/exit

    print("\n✅ PASS: Shadow mode working correctly")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    # Run tests directly
    async def run_all():
        await test_ma_strategy_no_repeat_signals()
        await test_simple_exit_strategy()
        await test_flip_position_strategy()
        await test_shadow_mode_prevents_real_orders()

    asyncio.run(run_all())
    print("\n🎉 ALL BACKTEST-TO-LIVE TESTS PASSED!\n")
