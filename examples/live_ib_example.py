"""Complete end-to-end example: IB live trading with strategy from backtest.

This example shows how to:
1. Define a lifecycle-compatible Strategy
2. Connect to Interactive Brokers
3. Subscribe to IB market data feed
4. Aggregate ticks to minute bars
5. Run strategy in shadow mode (no real orders)

Strategy: Simple moving average crossover
Data: Real-time IB market data for SPY
Mode: Shadow mode (tracks orders virtually)

Prerequisites:
    - TWS or IB Gateway running on the configured paper port
    - Paper account authenticated with API access enabled
    - SPY market-data permission

Expected Output:
    A bounded shadow session with IB ticks, one-minute bars, signals, and virtual positions.

Expected Failure:
    An unreachable session, wrong account, failed authentication, or unavailable market data
    terminates the run without placing an order at IB.

Cleanup:
    The bounded run stops the engine and closes broker and feed resources in a `finally` block.
"""

import asyncio
import logging
import os
from datetime import datetime

from ml4t.backtest import OrderSide, Strategy

from ml4t.live import LiveEngine, LiveRiskConfig
from ml4t.live.brokers.ib import IBBroker
from ml4t.live.feeds import BarAggregator, IBDataFeed
from ml4t.live.safety import SafeBroker

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)
DURATION_SECONDS = int(os.environ.get("ML4T_EXAMPLE_DURATION_SECONDS", "90"))


# ============================================================================
# STRATEGY DEFINITION (portable decision logic)
# ============================================================================


class SimpleMAStrategy(Strategy):
    """Simple moving average crossover strategy.

    Logic:
    - Calculate 10-period and 30-period moving averages
    - Buy when fast MA crosses above slow MA
    - Sell when fast MA crosses below slow MA

    This strategy uses lifecycle-v1 callbacks and portable broker operations.
    """

    def __init__(self, fast_period: int = 10, slow_period: int = 30):
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.prices: list[float] = []

    def on_start(self, broker):
        """Called when engine starts."""
        logger.info(f"Strategy started: MA({self.fast_period}, {self.slow_period})")

    def on_data(self, timestamp: datetime, data: dict, context: dict, broker):
        """Called for each bar.

        The callback signature is shared with the backtest runtime. Live data,
        broker behavior, latency, fills, and risk decisions remain distinct.

        Args:
            timestamp: Bar timestamp
            data: {symbol: {'open', 'high', 'low', 'close', 'volume'}}
            context: Additional metadata
            broker: Broker instance (sync interface via ThreadSafeBrokerWrapper)
        """
        # Get SPY data
        spy_bar = data.get("SPY")
        if not spy_bar:
            return

        close = spy_bar["close"]
        self.prices.append(close)

        # Need enough history
        if len(self.prices) < self.slow_period:
            logger.info(f"Building history: {len(self.prices)}/{self.slow_period}")
            return

        # Calculate MAs
        fast_ma = sum(self.prices[-self.fast_period :]) / self.fast_period
        slow_ma = sum(self.prices[-self.slow_period :]) / self.slow_period

        # Get current position
        position = broker.get_position("SPY")
        has_position = position is not None and position.quantity > 0

        logger.info(
            f"Bar: ${close:.2f} | Fast MA: ${fast_ma:.2f} | "
            f"Slow MA: ${slow_ma:.2f} | Position: {position.quantity if position else 0}"
        )

        # Trading logic
        if fast_ma > slow_ma and not has_position:
            # Bullish crossover - buy
            logger.info("BUY signal: fast MA crossed above slow MA")
            broker.submit_order("SPY", 100, side=OrderSide.BUY)

        elif fast_ma < slow_ma and has_position:
            # Bearish crossover - sell
            logger.info("SELL signal: fast MA crossed below slow MA")
            broker.submit_order("SPY", 100, side=OrderSide.SELL)

    def on_end(self, broker):
        """Called when engine stops."""
        logger.info("Strategy stopped")
        positions = broker.positions
        logger.info(f"Final positions: {positions}")


# ============================================================================
# LIVE TRADING SETUP
# ============================================================================


async def stop_after(duration: int, engine: LiveEngine) -> None:
    """Stop the engine after the documented bound."""
    await asyncio.sleep(duration)
    await engine.stop()


async def main() -> int:
    """Run the bounded IB shadow workflow."""

    # Step 1: Connect to Interactive Brokers
    logger.info("=" * 60)
    logger.info("Step 1: Connecting to Interactive Brokers...")
    logger.info("=" * 60)

    broker = IBBroker(
        host=os.environ.get("IB_HOST", "127.0.0.1"),
        port=int(os.environ.get("IB_PORT", "7497")),
        client_id=int(os.environ.get("IB_CLIENT_ID", "78")),
    )

    # Step 2: Create market data feed
    logger.info("=" * 60)
    logger.info("Step 2: Creating market data feed...")
    logger.info("=" * 60)

    # IB tick-level data
    ib_feed = IBDataFeed(
        ib=broker.ib,
        symbols=["SPY"],
        tick_throttle_ms=1000,  # Emit at most once per second
        experimental=True,
    )

    # Wrap with bar aggregator for minute bars
    feed = BarAggregator(
        source_feed=ib_feed,
        bar_size_minutes=1,
        assets=["SPY"],
    )

    logger.info("Feed created: IB ticks to 1-minute bars")

    # Step 3: Configure risk management
    logger.info("=" * 60)
    logger.info("Step 3: Configuring risk management...")
    logger.info("=" * 60)

    risk_config = LiveRiskConfig(
        execution_mode="shadow",
        max_position_value=50_000.0,  # $50k max position
        max_order_value=10_000.0,  # $10k max single order
        max_orders_per_minute=10,  # Rate limiting
    )

    safe_broker = SafeBroker(broker, risk_config)
    logger.info("Risk controls configured (shadow mode - no broker orders)")

    # Step 4: Create strategy
    logger.info("=" * 60)
    logger.info("Step 4: Initializing strategy...")
    logger.info("=" * 60)

    strategy = SimpleMAStrategy(fast_period=10, slow_period=30)
    logger.info("Strategy initialized: MA(10, 30)")

    # Step 5: Create and start engine
    logger.info("=" * 60)
    logger.info("Step 5: Starting live engine...")
    logger.info("=" * 60)

    engine = LiveEngine(
        strategy=strategy,
        broker=safe_broker,
        feed=feed,
    )

    try:
        await engine.connect()
    except Exception as exc:
        logger.error("IB paper shadow setup failed: %s", exc)
        return 1
    logger.info("Engine connected to the configured IB paper session")

    # Step 6: Run!
    logger.info("=" * 60)
    logger.info("SHADOW SESSION ACTIVE - bounded to %ss", DURATION_SECONDS)
    logger.info("=" * 60)

    stop_task = asyncio.create_task(stop_after(DURATION_SECONDS, engine))
    try:
        await engine.run()
    except Exception:
        logger.exception("IB shadow session failed")
        return 1
    finally:
        # Step 7: Clean shutdown
        logger.info("=" * 60)
        logger.info("Shutting down...")
        logger.info("=" * 60)

        await engine.stop()
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)

        # Print final stats
        logger.info("\nEngine Statistics:")
        for key, value in engine.stats.items():
            logger.info(f"  {key}: {value}")

        logger.info("\nFeed Statistics:")
        for key, value in feed.stats.items():
            logger.info(f"  {key}: {value}")

        logger.info("\nShutdown complete")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
