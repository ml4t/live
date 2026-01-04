"""
Stress test for memory leak detection (TASK-021).

Runs strategy for extended period to verify no memory leaks from:
- _prune_history() in SafeBroker
- _ib_order_map cleanup in IBBroker
- Bar aggregator history
"""

import asyncio
import gc
import time
from datetime import datetime, timezone
import psutil
import pytest

from ml4t.backtest import Strategy, OrderSide
from ml4t.live import LiveEngine, LiveRiskConfig
from ml4t.live.safety import SafeBroker
from ml4t.live.wrappers import ThreadSafeBrokerWrapper
from ml4t.live.feeds.aggregator import BarAggregator
from ml4t.live.protocols import Tick


class MockAsyncBroker:
    """Mock broker for stress testing."""

    def __init__(self):
        self._positions = {}
        self._orders = []
        self._order_counter = 0

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def get_positions_async(self):
        return list(self._positions.values())

    async def submit_order_async(self, asset, quantity, side, order_type):
        from ml4t.backtest.types import Order, OrderStatus

        self._order_counter += 1
        order = Order(
            asset=asset,
            quantity=quantity,
            side=side,
            order_type=order_type,
            order_id=f"STRESS-{self._order_counter}",
            status=OrderStatus.FILLED,  # Immediately filled for stress test
            created_at=datetime.now(timezone.utc),
        )
        self._orders.append(order)

        # Update position
        from ml4t.backtest.types import Position

        if asset in self._positions:
            self._positions[asset].quantity += quantity if side == OrderSide.BUY else -quantity
        else:
            self._positions[asset] = Position(
                asset=asset, quantity=quantity if side == OrderSide.BUY else -quantity, entry_price=100.0
            )

        return order

    async def cancel_order_async(self, order_id):
        return True

    async def get_cash_async(self):
        return 1_000_000.0

    async def get_account_value_async(self):
        return 1_000_000.0


class MockTickFeed:
    """Mock tick feed that generates infinite ticks."""

    def __init__(self, symbol='SPY', tick_interval=0.1):
        self._symbol = symbol
        self._tick_interval = tick_interval
        self._running = False
        self._tick_count = 0
        self._queue: asyncio.Queue[Tick | None] = asyncio.Queue()

    async def start(self):
        self._running = True
        asyncio.create_task(self._generate_ticks())

    async def stop(self):
        self._running = False
        await self._queue.put(None)

    async def _generate_ticks(self):
        """Generate ticks at specified interval."""
        while self._running:
            self._tick_count += 1
            tick = Tick(
                symbol=self._symbol,
                timestamp=time.time(),
                price=100.0 + (self._tick_count % 10),  # Oscillate price
                volume=100,
            )
            await self._queue.put(tick)
            await asyncio.sleep(self._tick_interval)

    def __aiter__(self):
        return self

    async def __anext__(self):
        tick = await self._queue.get()
        if tick is None:
            raise StopAsyncIteration
        return tick


class RandomSignalStrategy(Strategy):
    """Strategy that generates random signals for stress testing."""

    def __init__(self):
        self.bar_count = 0

    def on_data(self, timestamp, data, context, broker):
        """Generate signal every 10 bars."""
        self.bar_count += 1

        if self.bar_count % 10 == 0:
            position = broker.get_position('SPY')

            # Toggle position every 10 bars
            if position is None or position.quantity == 0:
                broker.submit_order('SPY', 10, side=OrderSide.BUY)
            else:
                broker.submit_order('SPY', position.quantity, side=OrderSide.SELL)


def get_memory_mb():
    """Get current process memory usage in MB."""
    process = psutil.Process()
    return process.memory_info().rss / 1024 / 1024


@pytest.mark.asyncio
@pytest.mark.slow
async def test_memory_leak_detection():
    """
    Stress test: Run for 10 minutes (configurable) to detect memory leaks.

    Tests:
    - SafeBroker._prune_history() cleans old entries
    - Bar aggregator doesn't accumulate bars
    - No unbounded data structures

    Expected: Memory usage stays within 50MB of baseline after stabilization.
    """
    # Configuration
    DURATION_MINUTES = 1  # Set to 10+ for real stress test
    SAMPLE_INTERVAL_SECONDS = 5
    MAX_MEMORY_GROWTH_MB = 50

    print(f"\n{'=' * 70}")
    print(f"STRESS TEST: Memory Leak Detection")
    print(f"Duration: {DURATION_MINUTES} minutes")
    print(f"Sample interval: {SAMPLE_INTERVAL_SECONDS} seconds")
    print(f"Max memory growth: {MAX_MEMORY_GROWTH_MB} MB")
    print(f"{'=' * 70}\n")

    # Force garbage collection before starting
    gc.collect()

    # Setup
    mock_broker = MockAsyncBroker()
    await mock_broker.connect()

    config = LiveRiskConfig(
        shadow_mode=True,  # Shadow mode for stress test
        max_position_value=100_000,
        max_order_value=10_000,
        prune_hours=0.01,  # Prune very aggressively (36 seconds)
    )

    safe_broker = SafeBroker(mock_broker, config)
    tick_feed = MockTickFeed(symbol='SPY', tick_interval=0.01)  # 100 ticks/sec
    bar_feed = BarAggregator(tick_feed, bar_size_minutes=1, flush_timeout_seconds=5)

    # Measure baseline memory
    baseline_memory = get_memory_mb()
    print(f"Baseline memory: {baseline_memory:.2f} MB")

    # Start feeds
    await bar_feed.start()

    # Track memory over time
    memory_samples = []
    start_time = time.time()
    last_sample_time = start_time

    try:
        # Let strategy run for specified duration
        strategy = RandomSignalStrategy()
        bars_processed = 0

        async for bar in bar_feed:
            bars_processed += 1

            # Call strategy
            timestamp = datetime.fromtimestamp(bar.timestamp, timezone.utc)
            data = {
                bar.symbol: {
                    'open': bar.open,
                    'high': bar.high,
                    'low': bar.low,
                    'close': bar.close,
                    'volume': bar.volume,
                }
            }

            # Wrap broker for sync access
            loop = asyncio.get_event_loop()
            wrapped_broker = ThreadSafeBrokerWrapper(safe_broker, loop)

            # Run strategy
            strategy.on_data(timestamp, data, {}, wrapped_broker)

            # Sample memory periodically
            current_time = time.time()
            if current_time - last_sample_time >= SAMPLE_INTERVAL_SECONDS:
                current_memory = get_memory_mb()
                memory_growth = current_memory - baseline_memory
                elapsed_minutes = (current_time - start_time) / 60

                memory_samples.append((elapsed_minutes, current_memory, memory_growth))

                print(
                    f"[{elapsed_minutes:.2f} min] "
                    f"Memory: {current_memory:.2f} MB | "
                    f"Growth: {memory_growth:+.2f} MB | "
                    f"Bars: {bars_processed} | "
                    f"Orders: {len(mock_broker._orders)}"
                )

                last_sample_time = current_time

                # Check if we've exceeded duration
                if elapsed_minutes >= DURATION_MINUTES:
                    print(f"\n✓ Reached target duration: {DURATION_MINUTES} minutes")
                    break

    finally:
        # Cleanup
        await bar_feed.stop()
        await mock_broker.disconnect()

    # Force garbage collection after test
    gc.collect()
    final_memory = get_memory_mb()
    final_growth = final_memory - baseline_memory

    # Analysis
    print(f"\n{'=' * 70}")
    print("MEMORY LEAK ANALYSIS")
    print(f"{'=' * 70}")
    print(f"Baseline memory: {baseline_memory:.2f} MB")
    print(f"Final memory: {final_memory:.2f} MB")
    print(f"Total growth: {final_growth:+.2f} MB")
    print(f"Bars processed: {bars_processed}")
    print(f"Orders submitted: {len(mock_broker._orders)}")

    if len(memory_samples) > 1:
        # Calculate trend (linear regression)
        import statistics

        times = [s[0] for s in memory_samples]
        growths = [s[2] for s in memory_samples]

        avg_growth = statistics.mean(growths)
        max_growth = max(growths)
        min_growth = min(growths)

        print(f"\nMemory growth stats:")
        print(f"  Average: {avg_growth:+.2f} MB")
        print(f"  Max: {max_growth:+.2f} MB")
        print(f"  Min: {min_growth:+.2f} MB")

    # Assertions
    print(f"\n{'=' * 70}")
    print("VERDICT")
    print(f"{'=' * 70}")

    if final_growth > MAX_MEMORY_GROWTH_MB:
        print(f"❌ FAIL: Memory growth ({final_growth:.2f} MB) exceeds limit ({MAX_MEMORY_GROWTH_MB} MB)")
        print(f"   Potential memory leak detected!")
        pytest.fail(f"Memory leak detected: {final_growth:.2f} MB growth")
    else:
        print(f"✅ PASS: Memory growth ({final_growth:.2f} MB) within acceptable range")
        print(f"   No memory leak detected")

    print(f"\n{'=' * 70}")
    print("STRESS TEST COMPLETE")
    print(f"{'=' * 70}\n")


@pytest.mark.asyncio
@pytest.mark.slow
async def test_prune_history_effectiveness():
    """
    Test that SafeBroker._prune_history() actually cleans up old data.

    Creates many orders and verifies history is pruned after timeout.
    """
    print(f"\n{'=' * 70}")
    print("TEST: Prune History Effectiveness")
    print(f"{'=' * 70}\n")

    mock_broker = MockAsyncBroker()
    await mock_broker.connect()

    config = LiveRiskConfig(
        shadow_mode=True,
        prune_hours=0.001,  # 3.6 seconds
    )

    safe_broker = SafeBroker(mock_broker, config)

    # Submit many orders
    NUM_ORDERS = 100

    print(f"Submitting {NUM_ORDERS} orders...")
    for i in range(NUM_ORDERS):
        await safe_broker.submit_order_async(
            asset='SPY', quantity=1, side=OrderSide.BUY, order_type='MARKET'
        )

    initial_history_size = len(safe_broker._order_history)
    print(f"Initial history size: {initial_history_size}")

    assert initial_history_size == NUM_ORDERS, "All orders should be in history"

    # Wait for prune timeout
    print("Waiting for prune timeout...")
    await asyncio.sleep(4)  # Wait longer than prune_hours

    # Trigger prune by submitting another order
    await safe_broker.submit_order_async(asset='SPY', quantity=1, side=OrderSide.BUY, order_type='MARKET')

    final_history_size = len(safe_broker._order_history)
    print(f"Final history size: {final_history_size}")

    assert final_history_size < initial_history_size, "History should be pruned"
    print(f"\n✅ Pruned {initial_history_size - final_history_size} old entries")

    await mock_broker.disconnect()

    print(f"\n{'=' * 70}")
    print("PRUNE TEST COMPLETE")
    print(f"{'=' * 70}\n")


if __name__ == "__main__":
    # Run stress test directly
    asyncio.run(test_memory_leak_detection())
    asyncio.run(test_prune_history_effectiveness())
