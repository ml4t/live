"""Experimental generic cryptocurrency market data feed via asynchronous CCXT.

This adapter is not part of the beta support contract. Exchange availability and payload behavior
depend on the installed CCXT implementation and require independent user validation.

Features:
- WebSocket streaming
- Multiple symbols
- OHLCV bars or trades
- Unified interface across exchanges

Example Binance:
    feed = CryptoFeed(
        exchange='binance',
        symbols=['BTC/USDT', 'ETH/USDT'],
        timeframe='1m',
        experimental=True,
    )
    await feed.start()

Example Coinbase:
    feed = CryptoFeed(
        exchange='coinbasepro',
        symbols=['BTC-USD', 'ETH-USD'],
        api_key=os.getenv('COINBASE_API_KEY'),
        api_secret=os.getenv('COINBASE_SECRET'),
        experimental=True,
    )
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, ClassVar

from ml4t.specs import (
    BarPayload,
    EventCompletion,
    LifecycleVersion,
    MarketEvent,
    MarketEventKind,
    TradePayload,
)

from ml4t.live.feeds.events import sequence_unavailable
from ml4t.live.feeds.experimental import require_experimental_opt_in
from ml4t.live.persistence import redact_sensitive
from ml4t.live.protocols import DataFeedProtocol

logger = logging.getLogger(__name__)

# CCXT is an optional dependency. Only async implementations are valid here.
ccxt: Any = None
CCXT_WEBSOCKET_AVAILABLE = False
try:
    import ccxt.pro as ccxt

    CCXT_AVAILABLE = True
    CCXT_WEBSOCKET_AVAILABLE = True
except ImportError:
    try:
        import ccxt.async_support as ccxt

        CCXT_AVAILABLE = True
    except ImportError:
        CCXT_AVAILABLE = False


CRYPTO_MISSING_GUARANTEES = (
    "bounded overload behavior",
    "provider continuity across reconnect",
    "credentialed exchange qualification",
)


class CryptoFeed(DataFeedProtocol):
    """Experimental cryptocurrency market data feed via asynchronous CCXT.

    Supports async REST polling and uses CCXT Pro websocket methods when available. No exchange,
    overload, reconnect, or performance guarantee is included in the beta support contract.

    Data Format:
        Experimental typed ``MarketEvent`` bars and trades with UTC timestamps.

    Exchange Symbols:
        - Binance: 'BTC/USDT', 'ETH/USDT'
        - Coinbase: 'BTC-USD', 'ETH-USD'
        - Kraken: 'BTC/USD', 'ETH/USD'

    Timeframes:
        '1m', '5m', '15m', '1h', '4h', '1d'

    Example WebSocket (Real-time):
        feed = CryptoFeed(
            exchange='binance',
            symbols=['BTC/USDT', 'ETH/USDT'],
            stream_trades=True,  # Stream trades (fastest)
            experimental=True,
        )

    Example OHLCV Bars:
        feed = CryptoFeed(
            exchange='binance',
            symbols=['BTC/USDT'],
            timeframe='1m',
            stream_ohlcv=True,
            experimental=True,
        )

    Example Authenticated:
        feed = CryptoFeed(
            exchange='binance',
            symbols=['BTC/USDT'],
            api_key='your-key',
            api_secret='your-secret',
            experimental=True,
        )
    """

    support_status: ClassVar[str] = "experimental"

    def __init__(
        self,
        exchange: str,
        symbols: list[str],
        *,
        timeframe: str = "1m",
        stream_trades: bool = False,
        stream_ohlcv: bool = True,
        api_key: str | None = None,
        api_secret: str | None = None,
        api_passphrase: str | None = None,
        experimental: bool = False,
    ) -> None:
        """Initialize crypto feed.

        Args:
            exchange: Exchange ID (e.g., 'binance', 'coinbasepro', 'kraken')
            symbols: Trading pairs (e.g., ['BTC/USDT', 'ETH/USDT'])
            timeframe: OHLCV timeframe ('1m', '5m', '1h', etc.)
            stream_trades: Stream trade ticks (faster updates)
            stream_ohlcv: Stream OHLCV candles
            api_key: API key (for authenticated endpoints)
            api_secret: API secret
            api_passphrase: API passphrase (Coinbase only)
            experimental: Must be true to acknowledge the unsupported feed contract.
        """
        require_experimental_opt_in(
            "CryptoFeed",
            experimental=experimental,
            missing_guarantees=CRYPTO_MISSING_GUARANTEES,
        )
        if not CCXT_AVAILABLE:
            raise ImportError(
                "ccxt package required. Install ml4t-live with its locked dependencies"
            )
        if not isinstance(exchange, str) or not exchange.strip():
            raise ValueError("exchange must be a non-empty string")
        if not symbols or any(
            not isinstance(symbol, str) or not symbol.strip() for symbol in symbols
        ):
            raise ValueError("symbols must contain at least one non-empty symbol")

        self.exchange_id = exchange
        self.symbols = list(symbols)
        self.timeframe = timeframe
        self.stream_trades = stream_trades
        self.stream_ohlcv = stream_ohlcv

        # Create exchange instance
        exchange_class = getattr(ccxt, exchange, None)
        if not callable(exchange_class):
            raise ValueError(f"CCXT exchange is unavailable: {exchange}")
        config = {
            "enableRateLimit": True,
        }

        if api_key:
            config["apiKey"] = api_key
        if api_secret:
            config["secret"] = api_secret
        if api_passphrase:
            config["password"] = api_passphrase

        self.exchange = exchange_class(config)

        # State
        self._queue: asyncio.Queue[MarketEvent | None] = asyncio.Queue()
        self._running = False
        self._stream_tasks: list[asyncio.Task] = []
        self._failure: RuntimeError | None = None
        self._completed_candles: set[tuple[str, int]] = set()
        self._evolving_candles: dict[tuple[str, int], BarPayload] = {}

        # Statistics
        self._tick_count = 0
        self._trade_count = 0
        self._candle_count = 0

    async def start(self) -> None:
        """Start streaming market data.

        Initiates WebSocket subscriptions for all symbols.
        """
        if self._running:
            return
        logger.info(f"CryptoFeed: Starting {self.exchange_id} feed for {len(self.symbols)} symbols")
        self._queue = asyncio.Queue()
        self._stream_tasks.clear()
        self._failure = None
        self._running = True

        # Load markets
        try:
            await self.exchange.load_markets()
        except BaseException:
            self._running = False
            raise

        # Start streaming tasks
        for symbol in self.symbols:
            if self.stream_trades:
                task = asyncio.create_task(self._stream_trades_for_symbol(symbol))
                self._stream_tasks.append(task)

            if self.stream_ohlcv:
                task = asyncio.create_task(self._stream_ohlcv_for_symbol(symbol))
                self._stream_tasks.append(task)

        logger.info(f"CryptoFeed: Started {len(self._stream_tasks)} stream(s)")

    def stop(self) -> None:
        """Stop streaming; use ``close`` to release the exchange connection."""
        logger.info("CryptoFeed: Stopping feed")
        self._running = False

        # Cancel all streaming tasks
        for task in self._stream_tasks:
            task.cancel()

        # Signal consumer
        self._signal_stop()

        logger.info(
            f"CryptoFeed: Stopped. "
            f"Ticks: {self._tick_count}, Trades: {self._trade_count}, "
            f"Candles: {self._candle_count}"
        )

    async def _stream_trades_for_symbol(self, symbol: str) -> None:
        """Stream trade ticks for a symbol.

        Uses WebSocket if available (ccxt.pro), else polling.
        """
        try:
            # Check if exchange supports WebSocket trades
            if hasattr(self.exchange, "watch_trades"):
                # WebSocket streaming (ccxt.pro)
                while self._running:
                    trades = await self.exchange.watch_trades(symbol)
                    for trade in trades:
                        await self._process_trade(trade, symbol)
            else:
                # Fallback: Poll REST API
                while self._running:
                    trades = await self.exchange.fetch_trades(symbol, limit=100)
                    for trade in trades:
                        await self._process_trade(trade, symbol)
                    await asyncio.sleep(1)  # Poll every second

        except asyncio.CancelledError:
            logger.info(f"CryptoFeed: Trade stream for {symbol} cancelled")
        except Exception as e:
            self._stream_failed(e, stream=f"trade:{symbol}")
            logger.error(
                "CryptoFeed: Error streaming trades for %s: %s",
                symbol,
                redact_sensitive(str(e)),
            )

    async def _stream_ohlcv_for_symbol(self, symbol: str) -> None:
        """Stream OHLCV candles for a symbol."""
        try:
            # Check if exchange supports WebSocket OHLCV
            if hasattr(self.exchange, "watch_ohlcv"):
                # WebSocket streaming (ccxt.pro)
                while self._running:
                    candles = await self.exchange.watch_ohlcv(
                        symbol,
                        self.timeframe,
                        limit=2,
                    )
                    await self._process_candle_batch(candles, symbol)
            else:
                # Fallback: Poll REST API
                while self._running:
                    candles = await self.exchange.fetch_ohlcv(symbol, self.timeframe, limit=2)
                    await self._process_candle_batch(candles, symbol)
                    await asyncio.sleep(5)  # Poll every 5 seconds

        except asyncio.CancelledError:
            logger.info(f"CryptoFeed: OHLCV stream for {symbol} cancelled")
        except Exception as e:
            self._stream_failed(e, stream=f"ohlcv:{symbol}")
            logger.error(
                "CryptoFeed: Error streaming OHLCV for %s: %s",
                symbol,
                redact_sensitive(str(e)),
            )

    async def _process_trade(self, trade: dict[str, Any], symbol: str) -> None:
        """Process and emit a trade tick.

        Args:
            trade: CCXT trade dict with keys: timestamp, price, amount, side, etc.
            symbol: Trading pair
        """
        receipt_time = datetime.now(UTC)
        timestamp = datetime.fromtimestamp(float(trade["timestamp"]) / 1000, tz=UTC)
        provider_sequence = trade.get("id")
        if provider_sequence is not None and (
            isinstance(provider_sequence, bool) or not isinstance(provider_sequence, str | int)
        ):
            provider_sequence = str(provider_sequence)
        event = MarketEvent(
            version=LifecycleVersion.V1,
            event_time=timestamp,
            receipt_time=receipt_time,
            kind=MarketEventKind.TRADE,
            completion=EventCompletion.COMPLETE,
            source=f"ccxt:{self.exchange_id}",
            asset=symbol,
            payload=TradePayload(float(trade["price"]), float(trade["amount"])),
            provider_sequence=provider_sequence,
            gap=(
                sequence_unavailable("CCXT", "trade identifier")
                if provider_sequence is None
                else None
            ),
            metadata={
                "experimental": True,
                "side": trade.get("side"),
            },
        )
        self._queue.put_nowait(event)
        self._trade_count += 1
        self._tick_count += 1

    async def _process_candle_batch(self, candles: list[list[Any]], symbol: str) -> None:
        """Emit prior candles as complete and the newest candle as evolving."""
        if not candles:
            return
        for candle in candles[:-1]:
            await self._process_candle(candle, symbol, completion=EventCompletion.COMPLETE)
        await self._process_candle(candles[-1], symbol, completion=EventCompletion.EVOLVING)

    async def _process_candle(
        self,
        candle: list[Any],
        symbol: str,
        *,
        completion: EventCompletion,
    ) -> None:
        """Process and emit an OHLCV candle.

        Args:
            candle: CCXT OHLCV array [timestamp, open, high, low, close, volume]
            symbol: Trading pair
        """
        if len(candle) < 6:
            raise ValueError("CCXT OHLCV candle must contain six fields")
        timestamp_ms = int(candle[0])
        timestamp = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
        payload = BarPayload(
            float(candle[1]),
            float(candle[2]),
            float(candle[3]),
            float(candle[4]),
            float(candle[5]),
        )
        key = (symbol, timestamp_ms)
        if completion is EventCompletion.COMPLETE and key in self._completed_candles:
            return
        if completion is EventCompletion.EVOLVING and (
            key in self._completed_candles or self._evolving_candles.get(key) == payload
        ):
            return
        event = MarketEvent(
            version=LifecycleVersion.V1,
            event_time=timestamp,
            receipt_time=datetime.now(UTC),
            kind=MarketEventKind.BAR,
            completion=completion,
            source=f"ccxt:{self.exchange_id}",
            asset=symbol,
            payload=payload,
            provider_sequence=str(timestamp_ms),
            metadata={
                "experimental": True,
                "timeframe": self.timeframe,
                "exchange": self.exchange_id,
            },
        )
        self._queue.put_nowait(event)
        if completion is EventCompletion.COMPLETE:
            self._completed_candles.add(key)
            self._evolving_candles.pop(key, None)
        else:
            self._evolving_candles[key] = payload
        self._candle_count += 1
        self._tick_count += 1

    def _stream_failed(self, error: Exception, *, stream: str) -> None:
        """Stop every producer and wake the consumer with the original cause."""
        if self._failure is not None:
            return
        failure = RuntimeError(f"CryptoFeed experimental {stream} stream failed")
        failure.__cause__ = error
        self._failure = failure
        self._running = False
        current = asyncio.current_task()
        for task in self._stream_tasks:
            if task is not current and not task.done():
                task.cancel()
        self._signal_stop()

    def _signal_stop(self) -> None:
        self._queue.put_nowait(None)

    async def __aiter__(self) -> AsyncIterator[MarketEvent]:
        """Async iterator yielding market data.

        Yields:
            Typed experimental market event.
        """
        while True:
            item = await self._queue.get()

            if item is None:  # Shutdown sentinel
                if self._failure is not None:
                    raise self._failure
                break

            yield item

    async def close(self) -> None:
        """Close exchange connection.

        Should be called in finally block.
        """
        await self.exchange.close()

    @property
    def stats(self) -> dict[str, Any]:
        """Get feed statistics."""
        return {
            "running": self._running,
            "exchange": self.exchange_id,
            "tick_count": self._tick_count,
            "trade_count": self._trade_count,
            "candle_count": self._candle_count,
            "symbols": self.symbols,
            "timeframe": self.timeframe,
            "experimental": True,
            "missing_beta_guarantees": list(CRYPTO_MISSING_GUARANTEES),
            "websocket_available": CCXT_WEBSOCKET_AVAILABLE,
        }
