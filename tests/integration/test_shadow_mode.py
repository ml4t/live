"""Integration test for shadow mode end-to-end flow.

Tests the complete live trading pipeline in shadow mode:
1. Connect to IB TWS/Gateway
2. Run strategy with real market data (or simulated ticks)
3. Strategy generates orders
4. SafeBroker intercepts orders (shadow mode)
5. VirtualPortfolio tracks positions
6. Verify NO real orders sent to broker

Requirements:
- TWS or IB Gateway running on port 7497
- Paper trading account
"""

import pytest
import asyncio
from datetime import datetime
from ml4t.backtest.types import OrderSide

from ml4t.live.safety import SafeBroker, LiveRiskConfig
from ml4t.live.feeds.aggregator import BarAggregator


# Mark all tests in this file as integration tests
pytestmark = pytest.mark.integration


# Client ID counter to ensure unique IDs per test
_client_id = 1000


def get_unique_client_id() -> int:
    """Get unique client ID for each test."""
    global _client_id
    _client_id += 1
    return _client_id


# === Simple Test Strategy ===


class SimpleTestStrategy:
    """Minimal strategy for testing shadow mode.

    Buys on first bar, sells on second bar.
    """

    def __init__(self, broker):
        self.broker = broker
        self.bar_count = 0
        self.bought = False

    async def on_data(self, data: dict):
        """Called when new bar arrives (async for SafeBroker).

        Args:
            data: Dictionary mapping asset to OHLCV dict
        """
        self.bar_count += 1

        # Get SPY data
        spy_bar = data.get("SPY")
        if not spy_bar:
            return

        print(f"\nBar {self.bar_count}: SPY close = ${spy_bar['close']:.2f}")

        # Buy on first bar
        if self.bar_count == 1 and not self.bought:
            print("  → Buying 10 shares of SPY")
            await self.broker.submit_order_async("SPY", 10, side=OrderSide.BUY)
            self.bought = True

        # Sell on second bar
        elif self.bar_count == 2 and self.bought:
            print("  → Selling 10 shares of SPY")
            await self.broker.submit_order_async("SPY", 10, side=OrderSide.SELL)


# === Connection Tests ===


@pytest.mark.asyncio
async def test_ib_connection(ib_broker):
    """Test basic connection to IB TWS/Gateway."""
    assert ib_broker.is_connected, "Should be connected to IB"

    # Get account info
    account_value = await ib_broker.get_account_value_async()
    cash = await ib_broker.get_cash_async()

    print("\n✅ Connected to IB paper account")
    print(f"   Account value: ${account_value:,.2f}")
    print(f"   Available cash: ${cash:,.2f}")

    assert account_value > 0, "Account value should be positive"
    assert cash > 0, "Cash should be positive"


@pytest.mark.asyncio
async def test_position_sync(ib_broker):
    """Test position synchronization from IB."""
    # Get positions
    positions = ib_broker.positions

    print("\n✅ Position sync successful")
    print(f"   Current positions: {len(positions)}")

    for asset, pos in positions.items():
        print(f"   - {asset}: {pos.quantity} shares @ ${pos.entry_price:.2f}")


# === Shadow Mode Tests ===


@pytest.mark.asyncio
async def test_shadow_mode_basic(ib_broker):
    """Test shadow mode with SafeBroker - orders NOT sent to IB."""
    broker = ib_broker

    # Create SafeBroker in shadow mode
    config = LiveRiskConfig(
        shadow_mode=True,  # CRITICAL: Shadow mode ON
        max_position_value=50_000.0,
        max_order_value=10_000.0,
    )
    safe_broker = SafeBroker(broker, config)

    # Record initial state
    initial_pending_orders = len(broker.pending_orders)
    initial_virtual_positions = len(safe_broker._virtual_portfolio.positions)

    print("\n📊 Initial state:")
    print(f"   Real pending orders: {initial_pending_orders}")
    print(f"   Virtual positions: {initial_virtual_positions}")

    # Submit order in shadow mode
    order = await safe_broker.submit_order_async(
        asset="SPY",
        quantity=10,
        side=OrderSide.BUY,
    )

    print("\n🔵 Submitted shadow order:")
    print(f"   Asset: {order.asset}")
    print(f"   Quantity: {order.quantity}")
    print(f"   Status: {order.status}")
    print(f"   Order ID: {order.order_id}")

    # Verify order marked as filled (shadow)
    assert order.status.value == "filled", "Shadow order should be marked as filled"
    assert "SHADOW" in order.order_id, "Shadow order ID should contain 'SHADOW'"

    # Wait a moment for any async updates
    await asyncio.sleep(1)

    # CRITICAL: Verify NO real order sent to IB
    current_pending_orders = len(broker.pending_orders)
    assert current_pending_orders == initial_pending_orders, (
        f"Real orders should not increase in shadow mode! "
        f"Was {initial_pending_orders}, now {current_pending_orders}"
    )

    # Verify VirtualPortfolio updated
    virtual_positions = safe_broker._virtual_portfolio.positions
    assert "SPY" in virtual_positions, "SPY position should exist in virtual portfolio"
    assert virtual_positions["SPY"].quantity == 10, "Virtual position should be 10 shares"

    print("\n✅ Shadow mode verification:")
    print(f"   Real pending orders: {current_pending_orders} (unchanged)")
    print(f"   Virtual positions: {len(virtual_positions)}")
    print(f"   Virtual SPY position: {virtual_positions['SPY'].quantity} shares")

    # Submit sell order
    await safe_broker.submit_order_async(
        asset="SPY",
        quantity=10,
        side=OrderSide.SELL,
    )

    print("\n🔴 Submitted shadow sell order")

    # Verify position closed in virtual portfolio
    await asyncio.sleep(0.5)
    virtual_positions = safe_broker._virtual_portfolio.positions
    assert "SPY" not in virtual_positions or virtual_positions["SPY"].quantity == 0, (
        "Virtual position should be closed after sell"
    )

    # Verify still no real orders
    assert len(broker.pending_orders) == initial_pending_orders, (
        "No real orders should be placed in shadow mode"
    )

    print("✅ Virtual position closed, no real orders placed")


@pytest.mark.asyncio
async def test_shadow_mode_with_aggregator(ib_broker):
    """Test shadow mode with BarAggregator feeding real-time bars."""
    broker = ib_broker

    # Create SafeBroker in shadow mode
    config = LiveRiskConfig(
        shadow_mode=True,
        max_position_value=50_000.0,
        max_order_value=10_000.0,
    )
    safe_broker = SafeBroker(broker, config)

    # Create mock feed that implements DataFeedProtocol
    class MockTickFeed:
        """Mock feed that implements DataFeedProtocol interface."""

        def __init__(self):
            self.started = False

        async def start(self):
            """Start feed (required by protocol)."""
            self.started = True

        def stop(self):
            """Stop feed (required by protocol)."""
            self.started = False

        async def __aiter__(self):
            """Async iterator yielding (timestamp, data, context) tuples."""
            # Generate ticks across 3 minutes to create 2 complete bars
            # (bar emits when next minute's tick arrives)
            base_time = datetime.now().replace(second=0, microsecond=0)
            base_price = 450.0

            # First minute: 3 ticks
            for i in range(3):
                timestamp = base_time.replace(second=i*20)
                data = {
                    "SPY": {
                        "price": base_price + i * 0.01,
                        "size": 100,
                    }
                }
                context = {}
                yield timestamp, data, context
                await asyncio.sleep(0.05)

            # Second minute: 3 ticks (first tick will emit bar 1)
            second_minute = base_time.replace(minute=base_time.minute + 1)
            for i in range(3):
                timestamp = second_minute.replace(second=i*20)
                data = {
                    "SPY": {
                        "price": base_price + 1.0 + i * 0.01,
                        "size": 100,
                    }
                }
                context = {}
                yield timestamp, data, context
                await asyncio.sleep(0.05)

            # Third minute: 1 tick to emit bar 2
            third_minute = base_time.replace(minute=base_time.minute + 2)
            timestamp = third_minute.replace(second=0)
            data = {
                "SPY": {
                    "price": base_price + 2.0,
                    "size": 100,
                }
            }
            context = {}
            yield timestamp, data, context

    # Create BarAggregator with mock feed
    mock_feed = MockTickFeed()
    aggregator = BarAggregator(
        source_feed=mock_feed,
        bar_size_minutes=1,
        assets=["SPY"],
    )

    # Start aggregator (this calls feed.start() and begins aggregation)
    await aggregator.start()

    # Create strategy
    strategy = SimpleTestStrategy(safe_broker)

    # Process bars (aggregator is directly async iterable)
    bar_count = 0
    async for timestamp, data, context in aggregator:
        if data is None:  # None sentinel signals end
            break

        bar_count += 1
        print(f"\n📊 Bar {bar_count} at {timestamp}: {data}")

        # Call strategy (now async)
        await strategy.on_data(data)

        # Limit to 2 bars for test
        if bar_count >= 2:
            aggregator.stop()
            break

    # Verify strategy executed
    assert strategy.bar_count >= 1, "Strategy should have processed at least 1 bar"
    assert strategy.bought, "Strategy should have bought"

    # Verify no real orders
    assert len(broker.pending_orders) == 0, "No real orders should be placed"

    # Verify virtual positions
    print("\n✅ Shadow mode with aggregator test complete")
    print(f"   Bars processed: {bar_count}")
    print(f"   Real orders: {len(broker.pending_orders)}")
    print(f"   Virtual positions: {len(safe_broker._virtual_portfolio.positions)}")


@pytest.mark.asyncio
async def test_shadow_mode_prevents_infinite_buy_loop(ib_broker):
    """Test that VirtualPortfolio prevents infinite buy loop."""
    broker = ib_broker

    # Create SafeBroker in shadow mode
    config = LiveRiskConfig(
        shadow_mode=True,
        max_position_value=50_000.0,
    )
    safe_broker = SafeBroker(broker, config)

    # Simulate strategy that checks positions and buys if not holding
    # This would cause infinite loop without VirtualPortfolio fix

    for i in range(3):
        print(f"\n🔄 Iteration {i + 1}")

        # Check if we have a position
        pos = safe_broker.get_position("SPY")
        has_position = pos is not None and pos.quantity > 0

        print(f"   Has SPY position: {has_position}")

        if not has_position:
            print("   → Buying 10 shares")
            await safe_broker.submit_order_async("SPY", 10, side=OrderSide.BUY)
        else:
            print(f"   → Already have {pos.quantity} shares, not buying")

        # Should only buy once, not 3 times
    virtual_pos = safe_broker._virtual_portfolio.positions.get("SPY")
    assert virtual_pos is not None, "Should have SPY position"
    assert virtual_pos.quantity == 10, (
        f"Should have exactly 10 shares, not {virtual_pos.quantity} (infinite loop bug!)"
    )

    print("\n✅ Infinite buy loop prevented!")
    print(f"   Final position: {virtual_pos.quantity} shares (expected: 10)")


@pytest.mark.asyncio
async def test_shadow_mode_risk_limits(ib_broker):
    """Test that risk limits are enforced in shadow mode."""
    broker = ib_broker

    # Create SafeBroker with tight limits
    config = LiveRiskConfig(
        shadow_mode=True,
        max_position_value=5_000.0,  # Only $5k per position
        max_order_value=2_000.0,  # Only $2k per order
    )
    safe_broker = SafeBroker(broker, config)

    # Try to place large order (should fail)
    from ml4t.live.safety import RiskLimitError

    with pytest.raises(RiskLimitError, match="Order value.*exceeds max"):
        # SPY ~$450, so 50 shares = $22,500 >> $2k limit
        await safe_broker.submit_order_async("SPY", 50, side=OrderSide.BUY)

    print("\n✅ Risk limits enforced in shadow mode")

    # Try smaller order (should succeed)
    order = await safe_broker.submit_order_async("SPY", 4, side=OrderSide.BUY)
    assert order.status.value == "filled"

    print(f"   Small order allowed: {order.quantity} shares")


# === Live Engine Integration (Commented - requires more setup) ===


# @pytest.mark.asyncio
# async def test_live_engine_shadow_mode():
#     """Test full LiveEngine with shadow mode.
#
#     NOTE: Commented out because it requires:
#     - Real-time tick feed from IB
#     - Market hours or simulated market data
#     - More complex setup
#
#     This would be the ultimate E2E test but is beyond scope of basic integration tests.
#     """
#     pass


if __name__ == "__main__":
    """Run integration tests manually."""
    import sys

    print("=" * 70)
    print("Shadow Mode Integration Tests")
    print("=" * 70)
    print("\nRequirements:")
    print("- TWS or IB Gateway running on port 7497")
    print("- Paper trading account")
    print("\nRunning tests...\n")

    # Run with pytest
    sys.exit(pytest.main([__file__, "-v", "-s"]))
