"""Interactive Brokers market data feed.

Provides real-time tick data from IB TWS/Gateway.

Features:
- Real-time tick-by-tick data
- Multiple symbols
- Automatic reconnection
- Tick buffering with asyncio.Queue

Example:
    ib = IB()
    await ib.connectAsync(...)

    feed = IBDataFeed(ib, symbols=['SPY', 'QQQ'])
    await feed.start()

    async for timestamp, data, context in feed:
        # data = {'SPY': {'price': 450.23, 'size': 100}, ...}
        strategy.on_data(timestamp, data, context, broker)
"""

import asyncio
import logging
from datetime import datetime
from typing import AsyncIterator, Any

from ib_async import IB, Stock, Ticker

from ml4t.live.protocols import DataFeedProtocol

logger = logging.getLogger(__name__)


class IBDataFeed(DataFeedProtocol):
    """Real-time market data feed from Interactive Brokers.

    Subscribes to tick-by-tick market data for specified symbols.
    Emits data as (timestamp, data, context) tuples.

    Data Format:
        timestamp: datetime - Tick timestamp
        data: dict[str, dict] - {symbol: {'price': float, 'size': int}}
        context: dict - Additional metadata (bid, ask, etc.)

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
        exchange: str = 'SMART',
        currency: str = 'USD',
        tick_throttle_ms: int = 100,  # Min time between emits
    ):
        """Initialize IB data feed.

        Args:
            ib: Connected IB instance
            symbols: List of symbols to subscribe to
            exchange: IB exchange (default: SMART routing)
            currency: Currency (default: USD)
            tick_throttle_ms: Minimum milliseconds between tick emissions
                (prevents overwhelming strategy with rapid ticks)
        """
        self.ib = ib
        self.symbols = symbols
        self.exchange = exchange
        self.currency = currency
        self.tick_throttle_ms = tick_throttle_ms

        # State
        self._queue: asyncio.Queue = asyncio.Queue()
        self._running = False
        self._contracts: dict[str, Stock] = {}
        self._tickers: dict[str, Ticker] = {}
        self._last_emit_time = 0.0

        # Statistics
        self._tick_count = 0
        self._throttled_count = 0

    async def start(self) -> None:
        """Subscribe to market data for all symbols.

        Creates contracts and subscribes to real-time tick data.

        Raises:
            RuntimeError: If IB not connected
        """
        if not self.ib.isConnected():
            raise RuntimeError("IB must be connected before starting feed")

        logger.info(f"IBDataFeed: Starting feed for {len(self.symbols)} symbols")
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
            ticker = self.ib.reqMktData(contract, '', False, False)
            self._tickers[symbol] = ticker

        # Register callback for ticker updates
        self.ib.pendingTickersEvent += self._on_pending_tickers

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
                logger.warning(f"IBDataFeed: Error canceling {symbol}: {e}")

        # Remove callback
        self.ib.pendingTickersEvent -= self._on_pending_tickers

        # Signal consumer to exit
        self._queue.put_nowait(None)

        logger.info(
            f"IBDataFeed: Stopped. "
            f"Ticks: {self._tick_count}, Throttled: {self._throttled_count}"
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
        self._tick_count += 1

        # Build data dict from all tickers
        timestamp = datetime.now()
        data: dict[str, dict] = {}
        context: dict[str, dict] = {}

        for ticker in tickers:
            if ticker.contract.symbol not in self.symbols:
                continue

            # Skip if no last price
            if ticker.last is None or ticker.last <= 0:
                continue

            symbol = ticker.contract.symbol

            # Core data (price, size)
            data[symbol] = {
                'price': float(ticker.last),
                'size': int(ticker.lastSize) if ticker.lastSize else 0,
            }

            # Extended context (bid, ask, volume)
            context[symbol] = {
                'bid': float(ticker.bid) if ticker.bid else None,
                'ask': float(ticker.ask) if ticker.ask else None,
                'bid_size': int(ticker.bidSize) if ticker.bidSize else 0,
                'ask_size': int(ticker.askSize) if ticker.askSize else 0,
                'volume': int(ticker.volume) if ticker.volume else 0,
            }

        # Emit only if we have data
        if data:
            self._queue.put_nowait((timestamp, data, context))

    async def __aiter__(self) -> AsyncIterator[tuple[datetime, dict, dict]]:
        """Async iterator yielding market data.

        Yields:
            Tuple of (timestamp, data, context) where:
            - timestamp: datetime of tick
            - data: {symbol: {'price': float, 'size': int}}
            - context: {symbol: {'bid', 'ask', 'bid_size', 'ask_size', 'volume'}}

        Stops when:
            - stop() is called (None sentinel)
            - Feed is not running
        """
        while self._running:
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
            'running': self._running,
            'tick_count': self._tick_count,
            'throttled_count': self._throttled_count,
            'symbols': self.symbols,
        }
