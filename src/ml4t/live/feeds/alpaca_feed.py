"""Alpaca Markets data feed.

Provides real-time market data from Alpaca for stocks and crypto.

Features:
- Real-time minute bars (default), quotes, or trades
- Both stocks and crypto supported
- Stream lifecycle managed by the Alpaca client; engine watchdog recovery is optional
- Data buffering with asyncio.Queue

Example:
    feed = AlpacaDataFeed(
        api_key='PKXXXXXXXX',
        secret_key='XXXXXXXXXX',
        symbols=['AAPL', 'GOOGL', 'BTC/USD'],
    )
    await feed.start()

    async for event in feed:
        consume(event)
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from alpaca.data.enums import DataFeed
from alpaca.data.live import CryptoDataStream, StockDataStream
from ml4t.specs import (
    BarPayload,
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


class AlpacaDataFeed(DataFeedProtocol):
    """Real-time market data feed from Alpaca Markets.

    Subscribes to real-time data for specified symbols.
    Supports both stocks and crypto.

    Data Types:
        bars: OHLCV minute bars (default, recommended for strategies)
        quotes: Bid/ask quotes (for spread-sensitive strategies)
        trades: Individual trades (highest frequency)

    Data Feeds:
        iex: Free tier (limited data)
        sip: Premium (full market data, requires subscription)

    Data Format:
        Validated ``MarketEvent`` bars, quotes, or trades with UTC event and receipt times.

    Example:
        # Stocks only
        feed = AlpacaDataFeed(
            api_key='PKXXXXXXXX',
            secret_key='XXXXXXXXXX',
            symbols=['AAPL', 'MSFT'],
        )

        # Mixed stocks and crypto
        feed = AlpacaDataFeed(
            api_key='PKXXXXXXXX',
            secret_key='XXXXXXXXXX',
            symbols=['AAPL', 'BTC/USD', 'ETH/USD'],
        )

        await feed.start()

        async for event in feed:
            consume(event)
    """

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        symbols: list[str],
        *,
        data_type: str = "bars",  # 'bars', 'quotes', 'trades'
        feed: str = "iex",  # 'iex' (free) or 'sip' (premium)
        queue_capacity: int = 1_024,
    ):
        """Initialize Alpaca data feed.

        Args:
            api_key: Alpaca API key
            secret_key: Alpaca secret key
            symbols: List of symbols (e.g., ['AAPL', 'BTC/USD'])
            data_type: Type of data - 'bars' (default), 'quotes', or 'trades'
            feed: Data feed type - 'iex' (free) or 'sip' (premium)
            queue_capacity: Maximum pending events before a fail-closed overflow.
        """
        if not symbols or any(
            not isinstance(symbol, str) or not symbol.strip() for symbol in symbols
        ):
            raise ValueError("symbols must contain at least one non-empty symbol")
        if data_type not in {"bars", "quotes", "trades"}:
            raise ValueError("data_type must be bars, quotes, or trades")
        if feed.lower() not in {"iex", "sip"}:
            raise ValueError("feed must be iex or sip")
        self._api_key = api_key
        self._secret_key = secret_key
        self._data_type = data_type
        self._feed = feed

        # Separate stock and crypto symbols
        self._stock_symbols = [s for s in symbols if not self._is_crypto(s)]
        self._crypto_symbols = [s for s in symbols if self._is_crypto(s)]

        # Streams (created in start())
        self._stock_stream: StockDataStream | None = None
        self._crypto_stream: CryptoDataStream | None = None
        self._stream_tasks: list[asyncio.Task] = []
        self._failure: Exception | None = None
        self._consumer_loop: asyncio.AbstractEventLoop | None = None

        # State
        self.queue_capacity = queue_capacity
        self._queue = BoundedEventQueue(capacity=queue_capacity, feed="alpaca")
        self._running = False
        self.max_event_age_seconds = 120.0 if data_type == "bars" else 30.0

        # Statistics
        self._bar_count = 0
        self._quote_count = 0
        self._trade_count = 0

    def _is_crypto(self, symbol: str) -> bool:
        """Check if symbol is crypto (e.g., BTC/USD).

        Args:
            symbol: Asset symbol

        Returns:
            True if symbol is crypto
        """
        return "/" in symbol and symbol.upper().endswith("/USD")

    async def start(self) -> None:
        """Subscribe to market data for all symbols.

        Creates streams and subscribes to real-time data.
        """
        logger.info(
            f"AlpacaDataFeed: Starting feed for "
            f"{len(self._stock_symbols)} stocks, {len(self._crypto_symbols)} crypto"
        )
        if self._running:
            return
        self._queue = BoundedEventQueue(capacity=self.queue_capacity, feed="alpaca")
        self._stream_tasks.clear()
        self._failure = None
        self._consumer_loop = asyncio.get_running_loop()
        self._running = True

        # Create stock stream if we have stock symbols
        if self._stock_symbols:
            # Convert string feed to DataFeed enum
            feed_enum = DataFeed.IEX if self._feed.lower() == "iex" else DataFeed.SIP
            self._stock_stream = StockDataStream(
                api_key=self._api_key,
                secret_key=self._secret_key,
                feed=feed_enum,
            )

            # Subscribe based on data type
            if self._data_type == "bars":
                self._stock_stream.subscribe_bars(self._on_stock_bar, *self._stock_symbols)
            elif self._data_type == "quotes":
                self._stock_stream.subscribe_quotes(self._on_stock_quote, *self._stock_symbols)
            elif self._data_type == "trades":
                self._stock_stream.subscribe_trades(self._on_stock_trade, *self._stock_symbols)

            # Start stream in background
            task = asyncio.create_task(self._run_stock_stream())
            self._stream_tasks.append(task)

        # Create crypto stream if we have crypto symbols
        if self._crypto_symbols:
            self._crypto_stream = CryptoDataStream(
                api_key=self._api_key,
                secret_key=self._secret_key,
            )

            # Subscribe based on data type
            if self._data_type == "bars":
                self._crypto_stream.subscribe_bars(self._on_crypto_bar, *self._crypto_symbols)
            elif self._data_type == "quotes":
                self._crypto_stream.subscribe_quotes(self._on_crypto_quote, *self._crypto_symbols)
            elif self._data_type == "trades":
                self._crypto_stream.subscribe_trades(self._on_crypto_trade, *self._crypto_symbols)

            # Start stream in background
            task = asyncio.create_task(self._run_crypto_stream())
            self._stream_tasks.append(task)

        logger.info("AlpacaDataFeed: Subscriptions started")

    def stop(self) -> None:
        """Stop data feed.

        Closes all streams and signals consumer to exit.
        """
        logger.info("AlpacaDataFeed: Stopping feed")
        self._running = False

        # Cancel stream tasks
        for task in self._stream_tasks:
            if not task.done():
                task.cancel()

        # Stop streams
        if self._stock_stream:
            try:
                self._stock_stream.stop()
            except Exception as e:
                logger.warning(
                    "AlpacaDataFeed: Error stopping stock stream: %s",
                    redact_sensitive(str(e)),
                )

        if self._crypto_stream:
            try:
                self._crypto_stream.stop()
            except Exception as e:
                logger.warning(
                    "AlpacaDataFeed: Error stopping crypto stream: %s",
                    redact_sensitive(str(e)),
                )

        # Signal consumer to exit
        self._queue.finish(discard=True)

        logger.info(
            f"AlpacaDataFeed: Stopped. "
            f"Bars: {self._bar_count}, Quotes: {self._quote_count}, Trades: {self._trade_count}"
        )

    # === Stock Handlers ===

    def _event(
        self,
        message: Any,
        *,
        kind: MarketEventKind,
        payload: BarPayload | QuotePayload | TradePayload,
        metadata: dict[str, Any],
    ) -> MarketEvent:
        event_time = utc_datetime(getattr(message, "timestamp", None), "Alpaca timestamp")
        provider_sequence = getattr(message, "sequence", None)
        if provider_sequence is None:
            provider_sequence = getattr(message, "id", None)
        if provider_sequence is not None and (
            isinstance(provider_sequence, bool) or not isinstance(provider_sequence, str | int)
        ):
            provider_sequence = str(provider_sequence)
        return MarketEvent(
            version=LifecycleVersion.V1,
            event_time=event_time,
            receipt_time=datetime.now(UTC),
            kind=kind,
            completion=(
                EventCompletion.COMPLETE
                if kind is MarketEventKind.BAR
                else EventCompletion.EVOLVING
            ),
            source="alpaca",
            asset=str(message.symbol).upper(),
            payload=payload,
            provider_sequence=provider_sequence,
            gap=(
                sequence_unavailable("Alpaca", f"{kind.value} stream")
                if provider_sequence is None
                else None
            ),
            metadata=metadata,
        )

    def _enqueue(self, event: MarketEvent) -> None:
        """Move provider-thread callbacks onto the engine event loop before buffering."""
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if self._consumer_loop is not None and current_loop is not self._consumer_loop:
            self._consumer_loop.call_soon_threadsafe(self._enqueue_on_consumer_loop, event)
            return
        self._enqueue_on_consumer_loop(event)

    def _enqueue_on_consumer_loop(self, event: MarketEvent) -> None:
        if not self._running:
            return
        try:
            self._queue.put_nowait(event)
        except FeedOverflowError as error:
            self._stream_failed(error)
            return
        if event.kind is MarketEventKind.BAR:
            self._bar_count += 1
        elif event.kind is MarketEventKind.QUOTE:
            self._quote_count += 1
        elif event.kind is MarketEventKind.TRADE:
            self._trade_count += 1

    async def _on_stock_bar(self, bar: Any) -> None:
        """Handle stock bar data.

        Args:
            bar: Alpaca Bar object
        """
        if not self._running:
            return

        event = self._event(
            bar,
            kind=MarketEventKind.BAR,
            payload=BarPayload(
                float(bar.open),
                float(bar.high),
                float(bar.low),
                float(bar.close),
                float(bar.volume),
            ),
            metadata={
                "vwap": float(bar.vwap) if getattr(bar, "vwap", None) is not None else None,
                "trade_count": (
                    int(bar.trade_count) if getattr(bar, "trade_count", None) is not None else None
                ),
                "market": "equity",
                "feed": self._feed,
            },
        )
        self._enqueue(event)

    async def _on_stock_quote(self, quote: Any) -> None:
        """Handle stock quote data.

        Args:
            quote: Alpaca Quote object
        """
        if not self._running:
            return

        event = self._event(
            quote,
            kind=MarketEventKind.QUOTE,
            payload=QuotePayload(
                float(quote.bid_price),
                float(quote.ask_price),
                float(quote.bid_size),
                float(quote.ask_size),
            ),
            metadata={"market": "equity", "feed": self._feed},
        )
        self._enqueue(event)

    async def _on_stock_trade(self, trade: Any) -> None:
        """Handle stock trade data.

        Args:
            trade: Alpaca Trade object
        """
        if not self._running:
            return

        event = self._event(
            trade,
            kind=MarketEventKind.TRADE,
            payload=TradePayload(float(trade.price), float(trade.size)),
            metadata={
                "exchange": str(trade.exchange) if getattr(trade, "exchange", None) else None,
                "conditions": [str(value) for value in (getattr(trade, "conditions", None) or [])],
                "market": "equity",
                "feed": self._feed,
            },
        )
        self._enqueue(event)

    # === Crypto Handlers ===

    async def _on_crypto_bar(self, bar: Any) -> None:
        """Handle crypto bar data.

        Args:
            bar: Alpaca CryptoBar object
        """
        if not self._running:
            return

        event = self._event(
            bar,
            kind=MarketEventKind.BAR,
            payload=BarPayload(
                float(bar.open),
                float(bar.high),
                float(bar.low),
                float(bar.close),
                float(bar.volume),
            ),
            metadata={
                "vwap": float(bar.vwap) if getattr(bar, "vwap", None) is not None else None,
                "trade_count": (
                    int(bar.trade_count) if getattr(bar, "trade_count", None) is not None else None
                ),
                "market": "crypto",
            },
        )
        self._enqueue(event)

    async def _on_crypto_quote(self, quote: Any) -> None:
        """Handle crypto quote data.

        Args:
            quote: Alpaca CryptoQuote object
        """
        if not self._running:
            return

        event = self._event(
            quote,
            kind=MarketEventKind.QUOTE,
            payload=QuotePayload(
                float(quote.bid_price),
                float(quote.ask_price),
                float(quote.bid_size),
                float(quote.ask_size),
            ),
            metadata={"market": "crypto"},
        )
        self._enqueue(event)

    async def _on_crypto_trade(self, trade: Any) -> None:
        """Handle crypto trade data.

        Args:
            trade: Alpaca CryptoTrade object
        """
        if not self._running:
            return

        event = self._event(
            trade,
            kind=MarketEventKind.TRADE,
            payload=TradePayload(float(trade.price), float(trade.size)),
            metadata={
                "taker_side": (
                    str(trade.taker_side) if getattr(trade, "taker_side", None) else None
                ),
                "market": "crypto",
            },
        )
        self._enqueue(event)

    # === Stream Runners ===

    async def _run_stock_stream(self) -> None:
        """Run stock data stream in background thread.

        Note: StockDataStream.run() calls asyncio.run() internally, so we must
        run it in a separate thread to avoid "cannot be called from a running
        event loop" errors.
        """
        try:
            logger.info("AlpacaDataFeed: Starting stock stream")
            if self._stock_stream:
                # Run in thread pool since .run() creates its own event loop
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._stock_stream.run)
        except asyncio.CancelledError:
            logger.info("AlpacaDataFeed: Stock stream cancelled")
            if self._stock_stream:
                self._stock_stream.stop()
        except Exception as e:
            self._stream_failed(e)
            logger.error("AlpacaDataFeed: Stock stream error: %s", redact_sensitive(str(e)))

    async def _run_crypto_stream(self) -> None:
        """Run crypto data stream in background thread.

        Note: CryptoDataStream.run() calls asyncio.run() internally, so we must
        run it in a separate thread to avoid "cannot be called from a running
        event loop" errors.
        """
        try:
            logger.info("AlpacaDataFeed: Starting crypto stream")
            if self._crypto_stream:
                # Run in thread pool since .run() creates its own event loop
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._crypto_stream.run)
        except asyncio.CancelledError:
            logger.info("AlpacaDataFeed: Crypto stream cancelled")
            if self._crypto_stream:
                self._crypto_stream.stop()
        except Exception as e:
            self._stream_failed(e)
            logger.error("AlpacaDataFeed: Crypto stream error: %s", redact_sensitive(str(e)))

    def _stream_failed(self, error: Exception) -> None:
        """Wake the engine when a provider stream exits with an error."""
        self._failure = error
        self._running = False
        current = asyncio.current_task()
        for task in self._stream_tasks:
            if task is not current and not task.done():
                task.cancel()
        for stream in (self._stock_stream, self._crypto_stream):
            if stream is not None:
                try:
                    stream.stop()
                except Exception as stop_error:
                    error.add_note(
                        f"stream stop also failed: {type(stop_error).__name__}: "
                        f"{redact_sensitive(str(stop_error))}"
                    )
        failure = RuntimeError("Alpaca stream failed")
        failure.__cause__ = error
        self._queue.fail(failure, discard=True)

    # === Async Iterator ===

    async def __aiter__(self) -> AsyncIterator[MarketEvent]:
        """Async iterator yielding market data.

        Yields:
            Validated bar, quote, or trade events.

        Stops when:
            - stop() is called (None sentinel)
            - Feed is not running
        """
        while True:
            item = await self._queue.get()

            # None sentinel signals shutdown
            if item is None:
                if self._failure is not None:
                    raise RuntimeError("Alpaca stream failed") from self._failure
                break

            yield item

    async def __anext__(self) -> MarketEvent:
        """Get next data item.

        Returns:
            A validated bar, quote, or trade event.

        Raises:
            StopAsyncIteration: When feed is stopped
        """
        queue_state = self._queue.snapshot()
        if not self._running and self._queue.empty() and not queue_state.failed:
            raise StopAsyncIteration

        item = await self._queue.get()
        if item is None:
            if self._failure is not None:
                raise RuntimeError("Alpaca stream failed") from self._failure
            raise StopAsyncIteration

        return item

    @property
    def stats(self) -> dict[str, Any]:
        """Get feed statistics.

        Returns:
            Dict with keys:
            - running: bool
            - bar_count: int
            - quote_count: int
            - trade_count: int
            - stock_symbols: list[str]
            - crypto_symbols: list[str]
        """
        return {
            "running": self._running,
            "bar_count": self._bar_count,
            "quote_count": self._quote_count,
            "trade_count": self._trade_count,
            "stock_symbols": self._stock_symbols,
            "crypto_symbols": self._crypto_symbols,
            "data_type": self._data_type,
            "feed": self._feed,
            "queue": self._queue.snapshot().to_dict(),
        }
