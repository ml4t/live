"""Alpaca Markets data feed.

Provides real-time market data from Alpaca for stocks and crypto.

Features:
- Real-time minute bars (default), quotes, or trades
- Both stocks and crypto supported
- Automatic reconnection via alpaca-py
- Data buffering with asyncio.Queue

Example:
    feed = AlpacaDataFeed(
        api_key='PKXXXXXXXX',
        secret_key='XXXXXXXXXX',
        symbols=['AAPL', 'GOOGL', 'BTC/USD'],
    )
    await feed.start()

    async for timestamp, data, context in feed:
        # data = {'AAPL': {'open': 150, 'high': 151, ...}, ...}
        strategy.on_data(timestamp, data, context, broker)
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from alpaca.data.enums import DataFeed
from alpaca.data.live import CryptoDataStream, StockDataStream

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
        timestamp: datetime - Bar/quote/trade timestamp
        data: dict[str, dict] - {symbol: {'open', 'high', 'low', 'close', 'volume'}}
        context: dict - Additional metadata

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

        async for timestamp, data, context in feed:
            strategy.on_data(timestamp, data, context, broker)
    """

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        symbols: list[str],
        *,
        data_type: str = "bars",  # 'bars', 'quotes', 'trades'
        feed: str = "iex",  # 'iex' (free) or 'sip' (premium)
    ):
        """Initialize Alpaca data feed.

        Args:
            api_key: Alpaca API key
            secret_key: Alpaca secret key
            symbols: List of symbols (e.g., ['AAPL', 'BTC/USD'])
            data_type: Type of data - 'bars' (default), 'quotes', or 'trades'
            feed: Data feed type - 'iex' (free) or 'sip' (premium)
        """
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

        # State
        self._queue: asyncio.Queue = asyncio.Queue()
        self._running = False

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
                logger.warning(f"AlpacaDataFeed: Error stopping stock stream: {e}")

        if self._crypto_stream:
            try:
                self._crypto_stream.stop()
            except Exception as e:
                logger.warning(f"AlpacaDataFeed: Error stopping crypto stream: {e}")

        # Signal consumer to exit
        self._queue.put_nowait(None)

        logger.info(
            f"AlpacaDataFeed: Stopped. "
            f"Bars: {self._bar_count}, Quotes: {self._quote_count}, Trades: {self._trade_count}"
        )

    # === Stock Handlers ===

    async def _on_stock_bar(self, bar: Any) -> None:
        """Handle stock bar data.

        Args:
            bar: Alpaca Bar object
        """
        if not self._running:
            return

        self._bar_count += 1
        symbol = bar.symbol.upper()

        timestamp = bar.timestamp if hasattr(bar, "timestamp") else datetime.now(timezone.utc)
        data = {
            symbol: {
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": int(bar.volume),
            }
        }
        context = {
            symbol: {
                "vwap": float(bar.vwap) if hasattr(bar, "vwap") and bar.vwap else None,
                "trade_count": int(bar.trade_count)
                if hasattr(bar, "trade_count") and bar.trade_count
                else None,
            }
        }

        self._queue.put_nowait((timestamp, data, context))

    async def _on_stock_quote(self, quote: Any) -> None:
        """Handle stock quote data.

        Args:
            quote: Alpaca Quote object
        """
        if not self._running:
            return

        self._quote_count += 1
        symbol = quote.symbol.upper()

        timestamp = quote.timestamp if hasattr(quote, "timestamp") else datetime.now(timezone.utc)

        # Calculate mid price
        bid = float(quote.bid_price) if quote.bid_price else 0.0
        ask = float(quote.ask_price) if quote.ask_price else 0.0
        mid = (bid + ask) / 2 if bid and ask else bid or ask

        data = {
            symbol: {
                "price": mid,
                "bid": bid,
                "ask": ask,
            }
        }
        context = {
            symbol: {
                "bid_size": int(quote.bid_size) if quote.bid_size else 0,
                "ask_size": int(quote.ask_size) if quote.ask_size else 0,
            }
        }

        self._queue.put_nowait((timestamp, data, context))

    async def _on_stock_trade(self, trade: Any) -> None:
        """Handle stock trade data.

        Args:
            trade: Alpaca Trade object
        """
        if not self._running:
            return

        self._trade_count += 1
        symbol = trade.symbol.upper()

        timestamp = trade.timestamp if hasattr(trade, "timestamp") else datetime.now(timezone.utc)
        data = {
            symbol: {
                "price": float(trade.price),
                "size": int(trade.size),
            }
        }
        context = {
            symbol: {
                "exchange": trade.exchange if hasattr(trade, "exchange") else None,
                "conditions": trade.conditions if hasattr(trade, "conditions") else None,
            }
        }

        self._queue.put_nowait((timestamp, data, context))

    # === Crypto Handlers ===

    async def _on_crypto_bar(self, bar: Any) -> None:
        """Handle crypto bar data.

        Args:
            bar: Alpaca CryptoBar object
        """
        if not self._running:
            return

        self._bar_count += 1
        symbol = bar.symbol.upper()

        timestamp = bar.timestamp if hasattr(bar, "timestamp") else datetime.now(timezone.utc)
        data = {
            symbol: {
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": float(bar.volume),  # Crypto uses float volume
            }
        }
        context = {
            symbol: {
                "vwap": float(bar.vwap) if hasattr(bar, "vwap") and bar.vwap else None,
                "trade_count": int(bar.trade_count)
                if hasattr(bar, "trade_count") and bar.trade_count
                else None,
            }
        }

        self._queue.put_nowait((timestamp, data, context))

    async def _on_crypto_quote(self, quote: Any) -> None:
        """Handle crypto quote data.

        Args:
            quote: Alpaca CryptoQuote object
        """
        if not self._running:
            return

        self._quote_count += 1
        symbol = quote.symbol.upper()

        timestamp = quote.timestamp if hasattr(quote, "timestamp") else datetime.now(timezone.utc)

        bid = float(quote.bid_price) if quote.bid_price else 0.0
        ask = float(quote.ask_price) if quote.ask_price else 0.0
        mid = (bid + ask) / 2 if bid and ask else bid or ask

        data = {
            symbol: {
                "price": mid,
                "bid": bid,
                "ask": ask,
            }
        }
        context = {
            symbol: {
                "bid_size": float(quote.bid_size) if quote.bid_size else 0.0,
                "ask_size": float(quote.ask_size) if quote.ask_size else 0.0,
            }
        }

        self._queue.put_nowait((timestamp, data, context))

    async def _on_crypto_trade(self, trade: Any) -> None:
        """Handle crypto trade data.

        Args:
            trade: Alpaca CryptoTrade object
        """
        if not self._running:
            return

        self._trade_count += 1
        symbol = trade.symbol.upper()

        timestamp = trade.timestamp if hasattr(trade, "timestamp") else datetime.now(timezone.utc)
        data = {
            symbol: {
                "price": float(trade.price),
                "size": float(trade.size),  # Crypto uses float
            }
        }
        context = {
            symbol: {
                "taker_side": trade.taker_side if hasattr(trade, "taker_side") else None,
            }
        }

        self._queue.put_nowait((timestamp, data, context))

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
            logger.error(f"AlpacaDataFeed: Stock stream error: {e}")

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
            logger.error(f"AlpacaDataFeed: Crypto stream error: {e}")

    # === Async Iterator ===

    async def __aiter__(self) -> AsyncIterator[tuple[datetime, dict, dict]]:
        """Async iterator yielding market data.

        Yields:
            Tuple of (timestamp, data, context) where:
            - timestamp: datetime of bar/quote/trade
            - data: {symbol: {'open', 'high', 'low', 'close', 'volume'}} for bars
            - context: {symbol: additional metadata}

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

    async def __anext__(self) -> tuple[datetime, dict[str, Any], dict[str, Any]]:
        """Get next data item.

        Returns:
            Tuple of (timestamp, data, context)

        Raises:
            StopAsyncIteration: When feed is stopped
        """
        if not self._running:
            raise StopAsyncIteration

        item = await self._queue.get()
        if item is None:
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
        }
