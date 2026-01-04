"""Cryptocurrency market data feed.

Generic crypto feed supporting multiple exchanges via CCXT.

Exchanges Supported:
- Binance (spot and futures)
- Coinbase Pro
- Kraken
- FTX (if still available)
- 100+ others via CCXT

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
    )
    await feed.start()

Example Coinbase:
    feed = CryptoFeed(
        exchange='coinbasepro',
        symbols=['BTC-USD', 'ETH-USD'],
        api_key=os.getenv('COINBASE_API_KEY'),
        api_secret=os.getenv('COINBASE_SECRET'),
    )
"""

import asyncio
import logging
from datetime import datetime
from typing import AsyncIterator, Any

from ml4t.live.protocols import DataFeedProtocol

logger = logging.getLogger(__name__)

# CCXT is optional dependency
try:
    import ccxt.pro as ccxt
    CCXT_AVAILABLE = True
except ImportError:
    try:
        import ccxt
        CCXT_AVAILABLE = True
        logger.warning("ccxt.pro not available, using sync ccxt (slower)")
    except ImportError:
        CCXT_AVAILABLE = False
        logger.warning("ccxt package not installed - CryptoFeed unavailable")


class CryptoFeed(DataFeedProtocol):
    """Cryptocurrency market data feed via CCXT.

    Provides unified interface to 100+ crypto exchanges.
    Supports both REST (polling) and WebSocket (streaming).

    Data Format:
        timestamp: datetime - Candle/trade timestamp
        data: dict[str, dict] - {symbol: {'open', 'high', 'low', 'close', 'volume'}}
        context: dict - Exchange-specific metadata

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
        )

    Example OHLCV Bars:
        feed = CryptoFeed(
            exchange='binance',
            symbols=['BTC/USDT'],
            timeframe='1m',
            stream_ohlcv=True,
        )

    Example Authenticated:
        feed = CryptoFeed(
            exchange='binance',
            symbols=['BTC/USDT'],
            api_key='your-key',
            api_secret='your-secret',
        )
    """

    def __init__(
        self,
        exchange: str,
        symbols: list[str],
        *,
        timeframe: str = '1m',
        stream_trades: bool = False,
        stream_ohlcv: bool = True,
        api_key: str | None = None,
        api_secret: str | None = None,
        api_passphrase: str | None = None,
    ):
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
        """
        if not CCXT_AVAILABLE:
            raise ImportError(
                "ccxt package required. Install with: pip install ccxt[asyncio]"
            )

        self.exchange_id = exchange
        self.symbols = symbols
        self.timeframe = timeframe
        self.stream_trades = stream_trades
        self.stream_ohlcv = stream_ohlcv

        # Create exchange instance
        exchange_class = getattr(ccxt, exchange)
        config = {
            'enableRateLimit': True,
        }

        if api_key:
            config['apiKey'] = api_key
        if api_secret:
            config['secret'] = api_secret
        if api_passphrase:
            config['password'] = api_passphrase

        self.exchange = exchange_class(config)

        # State
        self._queue: asyncio.Queue = asyncio.Queue()
        self._running = False
        self._stream_tasks: list[asyncio.Task] = []

        # Statistics
        self._tick_count = 0
        self._trade_count = 0
        self._candle_count = 0

    async def start(self) -> None:
        """Start streaming market data.

        Initiates WebSocket subscriptions for all symbols.
        """
        logger.info(
            f"CryptoFeed: Starting {self.exchange_id} feed for {len(self.symbols)} symbols"
        )
        self._running = True

        # Load markets
        await self.exchange.load_markets()

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
        """Stop streaming and close exchange connection."""
        logger.info("CryptoFeed: Stopping feed")
        self._running = False

        # Cancel all streaming tasks
        for task in self._stream_tasks:
            task.cancel()

        # Signal consumer
        self._queue.put_nowait(None)

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
            if hasattr(self.exchange, 'watch_trades'):
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
            logger.error(f"CryptoFeed: Error streaming trades for {symbol}: {e}")

    async def _stream_ohlcv_for_symbol(self, symbol: str) -> None:
        """Stream OHLCV candles for a symbol."""
        try:
            # Check if exchange supports WebSocket OHLCV
            if hasattr(self.exchange, 'watch_ohlcv'):
                # WebSocket streaming (ccxt.pro)
                while self._running:
                    candles = await self.exchange.watch_ohlcv(symbol, self.timeframe)
                    # Only emit latest complete candle
                    if candles:
                        await self._process_candle(candles[-1], symbol)
            else:
                # Fallback: Poll REST API
                last_timestamp = None
                while self._running:
                    candles = await self.exchange.fetch_ohlcv(
                        symbol, self.timeframe, limit=2
                    )
                    if candles:
                        latest = candles[-1]
                        # Only emit if new candle
                        if latest[0] != last_timestamp:
                            await self._process_candle(latest, symbol)
                            last_timestamp = latest[0]
                    await asyncio.sleep(5)  # Poll every 5 seconds

        except asyncio.CancelledError:
            logger.info(f"CryptoFeed: OHLCV stream for {symbol} cancelled")
        except Exception as e:
            logger.error(f"CryptoFeed: Error streaming OHLCV for {symbol}: {e}")

    async def _process_trade(self, trade: dict, symbol: str) -> None:
        """Process and emit a trade tick.

        Args:
            trade: CCXT trade dict with keys: timestamp, price, amount, side, etc.
            symbol: Trading pair
        """
        self._trade_count += 1
        self._tick_count += 1

        # Extract timestamp (milliseconds)
        timestamp = datetime.fromtimestamp(trade['timestamp'] / 1000)

        # Build data
        data = {
            symbol: {
                'price': float(trade['price']),
                'size': float(trade['amount']),
            }
        }

        # Context
        context = {
            symbol: {
                'side': trade.get('side'),  # 'buy' or 'sell'
                'trade_id': trade.get('id'),
            }
        }

        self._queue.put_nowait((timestamp, data, context))

    async def _process_candle(self, candle: list, symbol: str) -> None:
        """Process and emit an OHLCV candle.

        Args:
            candle: CCXT OHLCV array [timestamp, open, high, low, close, volume]
            symbol: Trading pair
        """
        self._candle_count += 1
        self._tick_count += 1

        # Extract fields
        timestamp = datetime.fromtimestamp(candle[0] / 1000)
        open_price = float(candle[1])
        high = float(candle[2])
        low = float(candle[3])
        close = float(candle[4])
        volume = float(candle[5])

        # Build data
        data = {
            symbol: {
                'open': open_price,
                'high': high,
                'low': low,
                'close': close,
                'volume': volume,
            }
        }

        # Context
        context = {
            symbol: {
                'timeframe': self.timeframe,
                'exchange': self.exchange_id,
            }
        }

        self._queue.put_nowait((timestamp, data, context))

    async def __aiter__(self) -> AsyncIterator[tuple[datetime, dict, dict]]:
        """Async iterator yielding market data.

        Yields:
            Tuple of (timestamp, data, context)
        """
        while self._running:
            item = await self._queue.get()

            if item is None:  # Shutdown sentinel
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
            'running': self._running,
            'exchange': self.exchange_id,
            'tick_count': self._tick_count,
            'trade_count': self._trade_count,
            'candle_count': self._candle_count,
            'symbols': self.symbols,
            'timeframe': self.timeframe,
        }
