"""LiveEngine - Async orchestration layer for live trading.

Bridges async infrastructure (brokers, data feeds) with synchronous Strategy.on_data().

Key Design:
1. Strategy runs in thread pool (via asyncio.to_thread)
2. ThreadSafeBrokerWrapper passed to strategy for sync broker calls
3. Graceful shutdown on SIGINT/SIGTERM
4. Configurable error handling

Thread Model:
- Main thread: asyncio event loop (broker I/O, data feed)
- Worker thread(s): Strategy.on_data() execution
- Communication: run_coroutine_threadsafe() via ThreadSafeBrokerWrapper

Example:
    engine = LiveEngine(strategy, broker, feed)
    await engine.connect()

    try:
        await engine.run()
    except KeyboardInterrupt:
        await engine.stop()
"""

import asyncio
import logging
import signal
from datetime import datetime
from typing import Any, Callable

from ml4t.backtest import Strategy

from .protocols import AsyncBrokerProtocol, DataFeedProtocol
from .wrappers import ThreadSafeBrokerWrapper

logger = logging.getLogger(__name__)


class LiveEngine:
    """Async live trading engine.

    Bridges async infrastructure with sync Strategy.on_data().

    Key Design Decisions:
    1. Strategy runs in thread pool (via asyncio.to_thread)
    2. ThreadSafeBrokerWrapper passed to strategy for sync broker calls
    3. Graceful shutdown on SIGINT/SIGTERM
    4. Configurable error handling

    Lifecycle:
    1. connect() - Connect to broker and data feed
    2. run() - Main loop (blocks until shutdown)
    3. stop() - Graceful shutdown

    Example:
        engine = LiveEngine(strategy, broker, feed)
        await engine.connect()

        try:
            await engine.run()
        except KeyboardInterrupt:
            await engine.stop()
    """

    def __init__(
        self,
        strategy: Strategy,
        broker: AsyncBrokerProtocol,
        feed: DataFeedProtocol,
        *,
        on_error: Callable[[Exception, datetime, dict], None] | None = None,
        halt_on_error: bool = False,
    ):
        """Initialize LiveEngine.

        Args:
            strategy: Strategy instance to execute
            broker: Async broker implementation (IBBroker, AlpacaBroker, etc.)
            feed: Data feed providing timestamp, data, context tuples
            on_error: Custom error handler callback. Signature:
                (error, timestamp, data) -> None
            halt_on_error: If True, stop engine on strategy error. If False,
                log error and continue.
        """
        self.strategy = strategy
        self.broker = broker
        self.feed = feed
        self.on_error = on_error or self._default_error_handler
        self.halt_on_error = halt_on_error

        # State
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wrapped_broker: ThreadSafeBrokerWrapper | None = None

        # Statistics
        self._bar_count = 0
        self._error_count = 0
        self._last_bar_time: datetime | None = None

    async def connect(self) -> None:
        """Connect to broker and data feed.

        Must be called before run().

        Steps:
        1. Connect broker (authenticate, subscribe to account updates)
        2. Start data feed (subscribe to market data)
        3. Create ThreadSafeBrokerWrapper for strategy use
        4. Install signal handlers for graceful shutdown
        """
        logger.info("LiveEngine: Connecting...")

        # Connect broker
        await self.broker.connect()

        # Start data feed
        await self.feed.start()

        # Create thread-safe wrapper
        self._loop = asyncio.get_running_loop()
        self._wrapped_broker = ThreadSafeBrokerWrapper(self.broker, self._loop)

        # Install signal handlers
        self._install_signal_handlers()

        logger.info("LiveEngine: Connected and ready")

    async def run(self) -> None:
        """Main async loop - receives bars and dispatches to strategy.

        Runs until:
        1. stop() is called
        2. Data feed ends
        3. Unrecoverable error (if halt_on_error=True)
        4. SIGINT/SIGTERM received

        Strategy Execution:
        - Strategy.on_data() runs in thread pool (asyncio.to_thread)
        - Receives ThreadSafeBrokerWrapper, not raw broker
        - Broker calls block the worker thread (not event loop)

        Error Handling:
        - Strategy exceptions caught and passed to on_error callback
        - If halt_on_error=True, engine stops
        - If halt_on_error=False, log and continue
        """
        if self._wrapped_broker is None:
            raise RuntimeError("Call connect() before run()")

        self._running = True
        logger.info("LiveEngine: Starting main loop")

        # Strategy lifecycle callback
        self.strategy.on_start(self._wrapped_broker)

        try:
            async for timestamp, data, context in self.feed:
                # Check for shutdown
                if self._shutdown_event.is_set():
                    logger.info("LiveEngine: Shutdown requested")
                    break

                self._bar_count += 1
                self._last_bar_time = timestamp

                try:
                    # Run strategy in thread pool to avoid blocking event loop
                    await asyncio.to_thread(
                        self.strategy.on_data,
                        timestamp,
                        data,
                        context,
                        self._wrapped_broker,
                    )
                except Exception as e:
                    self._error_count += 1
                    self.on_error(e, timestamp, data)

                    if self.halt_on_error:
                        logger.error("LiveEngine: Halting due to strategy error")
                        break

        except asyncio.CancelledError:
            logger.info("LiveEngine: Cancelled")
        finally:
            self._running = False
            self.strategy.on_end(self._wrapped_broker)
            logger.info(
                f"LiveEngine: Stopped. Bars: {self._bar_count}, Errors: {self._error_count}"
            )

    async def stop(self) -> None:
        """Graceful shutdown.

        Steps:
        1. Set shutdown event (signals main loop to exit)
        2. Stop data feed (no more bars)
        3. Disconnect broker (cancel subscriptions, close connection)

        Safe to call multiple times.
        """
        logger.info("LiveEngine: Stopping...")
        self._shutdown_event.set()

        # Stop data feed
        self.feed.stop()

        # Disconnect broker
        await self.broker.disconnect()

        logger.info("LiveEngine: Stopped")

    def _install_signal_handlers(self) -> None:
        """Install SIGINT/SIGTERM handlers for graceful shutdown.

        When CTRL+C or kill signal received:
        1. Set shutdown event
        2. Main loop exits cleanly
        3. Strategy.on_end() is called

        Note: Windows doesn't support add_signal_handler, so we catch
        NotImplementedError and silently skip (user can call stop() manually).
        """
        loop = asyncio.get_running_loop()

        def handler(sig: signal.Signals) -> None:
            logger.info(f"LiveEngine: Received {sig.name}")
            self._shutdown_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, handler, sig)
            except NotImplementedError:
                # Windows doesn't support add_signal_handler
                pass

    def _default_error_handler(
        self, error: Exception, timestamp: datetime, data: dict
    ) -> None:
        """Default error handler - log and continue.

        Args:
            error: Exception raised by strategy
            timestamp: Bar timestamp when error occurred
            data: Bar data (asset -> OHLCV dict)
        """
        logger.error(
            f"Strategy error at {timestamp}: {type(error).__name__}: {error}",
            exc_info=True,
        )

    @property
    def stats(self) -> dict[str, Any]:
        """Get engine statistics.

        Returns:
            Dict with keys:
            - running: bool - Is engine running?
            - bar_count: int - Total bars processed
            - error_count: int - Total strategy errors
            - last_bar_time: datetime | None - Timestamp of last bar
        """
        return {
            "running": self._running,
            "bar_count": self._bar_count,
            "error_count": self._error_count,
            "last_bar_time": self._last_bar_time,
        }
