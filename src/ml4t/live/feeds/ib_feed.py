"""Interactive Brokers market data feed.

Provides real-time tick data from IB TWS/Gateway.

Features:
- Real-time tick-by-tick data
- Multiple symbols
- Engine-level watchdog recovery can restart the feed when configured
- Tick buffering with asyncio.Queue

Example:
    ib = IB()
    await ib.connectAsync(...)

    feed = IBDataFeed(ib, symbols=['SPY', 'QQQ'])
    await feed.start()

    async for event in feed:
        consume(event)
"""

import asyncio
import logging
import math
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from ib_async import IB, Stock, Ticker
from ml4t.specs import (
    EventCompletion,
    LifecycleVersion,
    MarketEvent,
    MarketEventKind,
    QuotePayload,
    TradePayload,
)

from ml4t.live.feeds.events import sequence_unavailable, utc_datetime
from ml4t.live.feeds.queue import BoundedEventQueue, FeedOverflowError
from ml4t.live.persistence import redact_sensitive
from ml4t.live.protocols import DataFeedProtocol

logger = logging.getLogger(__name__)


class IBDataFeed(DataFeedProtocol):
    """Real-time market data feed from Interactive Brokers.

    Subscribes to tick-by-tick market data for specified symbols.
    Emits validated trade and quote events.

    Data Format:
        ``MarketEvent`` trade and quote snapshots with UTC provider or receipt time.

    Note:
        - IB must be connected before creating feed
        - Requires market data subscription for symbols
        - Throttles rapid ticks to avoid overwhelming strategy

    Example:
        ib = IB()
        await ib.connectAsync('127.0.0.1', 7497, clientId=1)

        feed = IBDataFeed(ib, symbols=['SPY', 'QQQ', 'IWM'])
        await feed.start()

        # Use directly or wrap with BarAggregator
        aggregator = BarAggregator(feed, bar_size_minutes=1)
    """

    def __init__(
        self,
        ib: IB,
        symbols: list[str],
        *,
        exchange: str = "SMART",
        currency: str = "USD",
        tick_throttle_ms: int = 100,  # Min time between emits
        queue_capacity: int = 1_024,
    ) -> None:
        """Initialize IB data feed.

        Args:
            ib: Connected IB instance
            symbols: List of symbols to subscribe to
            exchange: IB exchange (default: SMART routing)
            currency: Currency (default: USD)
            tick_throttle_ms: Minimum milliseconds between tick emissions
                (prevents overwhelming strategy with rapid ticks)
            queue_capacity: Maximum pending events before a fail-closed overflow.
        """
        if not symbols or any(
            not isinstance(symbol, str) or not symbol.strip() for symbol in symbols
        ):
            raise ValueError("symbols must contain at least one non-empty symbol")
        if (
            isinstance(tick_throttle_ms, bool)
            or not isinstance(tick_throttle_ms, int | float)
            or not math.isfinite(tick_throttle_ms)
            or tick_throttle_ms < 0
        ):
            raise ValueError("tick_throttle_ms must be finite and non-negative")
        self.ib = ib
        self.symbols = list(symbols)
        self.exchange = exchange
        self.currency = currency
        self.tick_throttle_ms = tick_throttle_ms

        # State
        self.queue_capacity = queue_capacity
        self._queue = BoundedEventQueue(capacity=queue_capacity, feed="interactive_brokers")
        self._running = False
        self._contracts: dict[str, Stock] = {}
        self._tickers: dict[str, Ticker] = {}
        self._last_emit_time = 0.0
        self._callback_registered = False
        self.max_event_age_seconds = 5.0

        # Statistics
        self._tick_count = 0
        self._throttled_count = 0
        self._rejected_count = 0

    async def start(self) -> None:
        """Subscribe to market data for all symbols.

        Creates contracts and subscribes to real-time tick data.

        Raises:
            RuntimeError: If IB not connected
        """
        if not self.ib.isConnected():
            raise RuntimeError("IB must be connected before starting feed")
        if self._running:
            return

        logger.info(f"IBDataFeed: Starting feed for {len(self.symbols)} symbols")
        self._queue = BoundedEventQueue(
            capacity=self.queue_capacity,
            feed="interactive_brokers",
        )
        self._contracts.clear()
        self._tickers.clear()
        self._running = True

        # Create contracts
        for symbol in self.symbols:
            contract = Stock(symbol, self.exchange, self.currency)
            self._contracts[symbol] = contract

            # Qualify contract (ensure IB recognizes it)
            qualified = await self.ib.qualifyContractsAsync(contract)
            if not qualified:
                logger.warning(f"IBDataFeed: Could not qualify contract for {symbol}")
                continue

            # Request market data
            ticker = self.ib.reqMktData(contract, "", False, False)
            self._tickers[symbol] = ticker

        # Register callback for ticker updates
        self.ib.pendingTickersEvent += self._on_pending_tickers
        self._callback_registered = True

        logger.info(f"IBDataFeed: Subscribed to {len(self._tickers)} symbols")

    def stop(self) -> None:
        """Unsubscribe from market data.

        Cancels all market data subscriptions and stops feed.
        """
        logger.info("IBDataFeed: Stopping feed")
        self._running = False

        # Unsubscribe from all tickers
        for symbol, contract in self._contracts.items():
            try:
                self.ib.cancelMktData(contract)
            except Exception as e:
                logger.warning(
                    "IBDataFeed: Error canceling %s: %s",
                    symbol,
                    redact_sensitive(str(e)),
                )

        # Remove callback
        if self._callback_registered:
            self.ib.pendingTickersEvent -= self._on_pending_tickers
            self._callback_registered = False

        # Signal consumer to exit
        self._queue.finish(discard=True)

        logger.info(
            f"IBDataFeed: Stopped. Ticks: {self._tick_count}, Throttled: {self._throttled_count}"
        )

    def _on_pending_tickers(self, tickers: list[Ticker]) -> None:
        """Callback when ticker data updates.

        Throttles rapid ticks to avoid overwhelming strategy.

        Args:
            tickers: List of updated Ticker objects
        """
        if not self._running:
            return

        # Check throttle
        now = asyncio.get_event_loop().time()
        if (now - self._last_emit_time) * 1000 < self.tick_throttle_ms:
            self._throttled_count += 1
            return

        self._last_emit_time = now
        receipt_time = datetime.now(UTC)

        for ticker in tickers:
            if ticker.contract.symbol not in self.symbols:
                continue
            timestamp = getattr(ticker, "time", None)
            if timestamp is None:
                event_time = receipt_time
                time_capability = "local receipt time; provider event time unavailable"
            else:
                try:
                    event_time = utc_datetime(timestamp, "IB ticker time")
                except (TypeError, ValueError) as error:
                    self._rejected_count += 1
                    logger.warning(
                        "IBDataFeed: Rejected ticker timestamp: %s", type(error).__name__
                    )
                    continue
                time_capability = "provider"

            metadata = {
                "event_time_capability": time_capability,
                "volume": float(ticker.volume) if ticker.volume is not None else None,
            }
            emitted = 0
            if ticker.last is not None:
                try:
                    self._enqueue(
                        MarketEvent(
                            version=LifecycleVersion.V1,
                            event_time=event_time,
                            receipt_time=receipt_time,
                            kind=MarketEventKind.TRADE,
                            completion=EventCompletion.EVOLVING,
                            source="interactive_brokers",
                            asset=str(ticker.contract.symbol).upper(),
                            payload=TradePayload(
                                float(ticker.last),
                                float(ticker.lastSize) if ticker.lastSize is not None else 0.0,
                            ),
                            gap=sequence_unavailable("IB", "pending ticker snapshot"),
                            metadata=metadata,
                        )
                    )
                    emitted += 1
                except (TypeError, ValueError) as error:
                    self._rejected_count += 1
                    logger.warning("IBDataFeed: Rejected trade snapshot: %s", type(error).__name__)
            if ticker.bid is not None or ticker.ask is not None:
                try:
                    self._enqueue(
                        MarketEvent(
                            version=LifecycleVersion.V1,
                            event_time=event_time,
                            receipt_time=receipt_time,
                            kind=MarketEventKind.QUOTE,
                            completion=EventCompletion.EVOLVING,
                            source="interactive_brokers",
                            asset=str(ticker.contract.symbol).upper(),
                            payload=QuotePayload(
                                float(ticker.bid),
                                float(ticker.ask),
                                float(ticker.bidSize) if ticker.bidSize is not None else 0.0,
                                float(ticker.askSize) if ticker.askSize is not None else 0.0,
                            ),
                            gap=sequence_unavailable("IB", "pending ticker snapshot"),
                            metadata=metadata,
                        )
                    )
                    emitted += 1
                except (TypeError, ValueError) as error:
                    self._rejected_count += 1
                    logger.warning("IBDataFeed: Rejected quote snapshot: %s", type(error).__name__)
            self._tick_count += emitted

    def _enqueue(self, event: MarketEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except FeedOverflowError:
            self._running = False
            raise

    async def __aiter__(self) -> AsyncIterator[MarketEvent]:
        """Async iterator yielding market data.

        Yields:
            Validated trade and quote events.

        Stops when:
            - stop() is called (None sentinel)
            - Feed is not running
        """
        while True:
            item = await self._queue.get()

            # None sentinel signals shutdown
            if item is None:
                break

            yield item

    @property
    def stats(self) -> dict[str, Any]:
        """Get feed statistics.

        Returns:
            Dict with keys:
            - running: bool
            - tick_count: int - Total ticks received
            - throttled_count: int - Ticks throttled
            - symbols: list[str] - Subscribed symbols
        """
        return {
            "running": self._running,
            "tick_count": self._tick_count,
            "throttled_count": self._throttled_count,
            "rejected_count": self._rejected_count,
            "symbols": self.symbols,
            "queue": self._queue.snapshot().to_dict(),
        }
