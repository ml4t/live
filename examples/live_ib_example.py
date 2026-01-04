"""Complete end-to-end example: IB live trading with strategy from backtest.

This example shows how to:
1. Define a Strategy (same as backtest)
2. Connect to Interactive Brokers
3. Subscribe to IB market data feed
4. Aggregate ticks to minute bars
5. Run strategy in shadow mode (no real orders)

Strategy: Simple moving average crossover
Data: Real-time IB market data for SPY
Mode: Shadow mode (tracks orders virtually)
"""

import asyncio
import logging
from datetime import datetime

from ml4t.backtest import Strategy, OrderSide

from ml4t.live import LiveEngine, LiveRiskConfig
from ml4t.live.brokers.ib import IBBroker
from ml4t.live.feeds import IBDataFeed, BarAggregator
from ml4t.live.safety import SafeBroker

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ============================================================================
# STRATEGY DEFINITION (Same as backtest - zero changes!)
# ============================================================================

class SimpleMAStrategy(Strategy):
    """Simple moving average crossover strategy.

    Logic:
    - Calculate 10-period and 30-period moving averages
    - Buy when fast MA crosses above slow MA
    - Sell when fast MA crosses below slow MA

    This is the SAME strategy you'd use in backtest!
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

        This method signature is IDENTICAL to backtest Strategy!

        Args:
            timestamp: Bar timestamp
            data: {symbol: {'open', 'high', 'low', 'close', 'volume'}}
            context: Additional metadata
            broker: Broker instance (sync interface via ThreadSafeBrokerWrapper)
        """
        # Get SPY data
        spy_bar = data.get('SPY')
        if not spy_bar:
            return

        close = spy_bar['close']
        self.prices.append(close)

        # Need enough history
        if len(self.prices) < self.slow_period:
            logger.info(f"Building history: {len(self.prices)}/{self.slow_period}")
            return

        # Calculate MAs
        fast_ma = sum(self.prices[-self.fast_period:]) / self.fast_period
        slow_ma = sum(self.prices[-self.slow_period:]) / self.slow_period

        # Get current position
        position = broker.get_position('SPY')
        has_position = position is not None and position.quantity > 0

        logger.info(
            f"Bar: ${close:.2f} | Fast MA: ${fast_ma:.2f} | "
            f"Slow MA: ${slow_ma:.2f} | Position: {position.quantity if position else 0}"
        )

        # Trading logic
        if fast_ma > slow_ma and not has_position:
            # Bullish crossover - buy
            logger.info("🚀 BUY Signal: Fast MA crossed above Slow MA")
            broker.submit_order('SPY', 100, side=OrderSide.BUY)

        elif fast_ma < slow_ma and has_position:
            # Bearish crossover - sell
            logger.info("📉 SELL Signal: Fast MA crossed below Slow MA")
            broker.submit_order('SPY', 100, side=OrderSide.SELL)

    def on_end(self, broker):
        """Called when engine stops."""
        logger.info("Strategy stopped")
        positions = broker.positions
        logger.info(f"Final positions: {positions}")


# ============================================================================
# LIVE TRADING SETUP
# ============================================================================

async def main():
    """Main entry point for live trading."""

    # Step 1: Connect to Interactive Brokers
    logger.info("=" * 60)
    logger.info("Step 1: Connecting to Interactive Brokers...")
    logger.info("=" * 60)

    broker = IBBroker(
        host='127.0.0.1',
        port=7497,  # Paper trading port (use 7496 for live)
        client_id=1,
    )

    await broker.connect()
    logger.info(f"✅ Connected to IB. Account: {broker._account}")

    # Step 2: Create market data feed
    logger.info("=" * 60)
    logger.info("Step 2: Creating market data feed...")
    logger.info("=" * 60)

    # IB tick-level data
    ib_feed = IBDataFeed(
        ib=broker.ib,
        symbols=['SPY'],
        tick_throttle_ms=1000,  # Emit at most once per second
    )

    # Wrap with bar aggregator for minute bars
    feed = BarAggregator(
        source_feed=ib_feed,
        bar_size_minutes=1,
        assets=['SPY'],
    )

    logger.info("✅ Feed created: IB ticks → 1-minute bars")

    # Step 3: Configure risk management
    logger.info("=" * 60)
    logger.info("Step 3: Configuring risk management...")
    logger.info("=" * 60)

    risk_config = LiveRiskConfig(
        shadow_mode=True,  # CRITICAL: Start with shadow mode!
        max_position_value=50_000.0,  # $50k max position
        max_order_value=10_000.0,     # $10k max single order
        max_orders_per_minute=10,     # Rate limiting
    )

    safe_broker = SafeBroker(broker, risk_config)
    logger.info("✅ Risk controls configured (SHADOW MODE - no real orders)")

    # Step 4: Create strategy
    logger.info("=" * 60)
    logger.info("Step 4: Initializing strategy...")
    logger.info("=" * 60)

    strategy = SimpleMAStrategy(fast_period=10, slow_period=30)
    logger.info("✅ Strategy initialized: MA(10, 30)")

    # Step 5: Create and start engine
    logger.info("=" * 60)
    logger.info("Step 5: Starting live engine...")
    logger.info("=" * 60)

    engine = LiveEngine(
        strategy=strategy,
        broker=safe_broker,
        feed=feed,
    )

    await engine.connect()
    logger.info("✅ Engine connected and ready")

    # Step 6: Run!
    logger.info("=" * 60)
    logger.info("LIVE TRADING ACTIVE - Press Ctrl+C to stop")
    logger.info("=" * 60)

    try:
        await engine.run()
    except KeyboardInterrupt:
        logger.info("\n⚠️  Shutdown requested by user")
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
    finally:
        # Step 7: Clean shutdown
        logger.info("=" * 60)
        logger.info("Shutting down...")
        logger.info("=" * 60)

        await engine.stop()
        await broker.disconnect()

        # Print final stats
        logger.info("\nEngine Statistics:")
        for key, value in engine.stats.items():
            logger.info(f"  {key}: {value}")

        logger.info("\nFeed Statistics:")
        for key, value in feed.stats.items():
            logger.info(f"  {key}: {value}")

        logger.info("\n✅ Shutdown complete")


if __name__ == '__main__':
    """
    Prerequisites:
    1. TWS or IB Gateway running
    2. API enabled (port 7497 for paper trading)
    3. Market data subscription for SPY

    To run:
        python examples/live_ib_example.py

    Expected output:
        - Connects to IB
        - Subscribes to SPY market data
        - Receives ticks, aggregates to 1-minute bars
        - Calculates moving averages
        - Generates buy/sell signals
        - Tracks positions VIRTUALLY (shadow mode)
        - NO REAL ORDERS PLACED

    Safety:
        - Shadow mode is enabled (shadow_mode=True)
        - All orders are virtual
        - Check broker.pending_orders == [] to verify
        - Check safe_broker._virtual_portfolio.positions for virtual positions

    Next steps:
        1. Run for 1-2 weeks in shadow mode
        2. Verify strategy logic is correct
        3. Change shadow_mode=False for paper trading
        4. Test with paper account for 2-4 weeks
        5. Gradually move to live with small positions
    """
    asyncio.run(main())
