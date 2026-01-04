"""OKX funding rate feed for crypto perpetuals.

Provides OHLCV bars plus funding rate data for perpetual swap contracts.
No API key required, no geo-restrictions.

Key Features:
- Hourly OHLCV bars for price data
- Funding rate updates (every 8 hours, polled hourly)
- Combined data + context for ML strategy consumption

Data Format:
    timestamp: Bar close time
    data: {symbol: {'open', 'high', 'low', 'close', 'volume'}}
    context: {symbol: {'funding_rate', 'next_funding_rate', 'next_funding_time'}}

Example:
    feed = OKXFundingFeed(
        symbols=['BTC-USDT-SWAP', 'ETH-USDT-SWAP'],
        timeframe='1H',
    )
    await feed.start()

    async for timestamp, data, context in feed:
        # data has OHLCV
        btc_close = data['BTC-USDT-SWAP']['close']
        # context has funding rate
        btc_funding = context['BTC-USDT-SWAP']['funding_rate']
"""

import asyncio
import logging
from datetime import datetime, UTC
from typing import Any

import httpx

from ml4t.live.protocols import DataFeedProtocol

logger = logging.getLogger(__name__)


class OKXFundingFeed(DataFeedProtocol):
    """OKX funding rate feed with OHLCV bars.

    Combines price data with funding rate information for ML strategies
    that trade crypto perpetual futures based on funding rate signals.

    Data Flow:
        1. Poll /market/candles for latest OHLCV bar
        2. Poll /public/funding-rate for current funding rate
        3. Combine into (timestamp, data, context) tuple
        4. Emit to strategy

    Symbol Format:
        OKX perpetual swaps use format: BTC-USDT-SWAP, ETH-USDT-SWAP
    """

    BASE_URL = "https://www.okx.com/api/v5"

    def __init__(
        self,
        symbols: list[str],
        *,
        timeframe: str = "1H",
        poll_interval_seconds: float = 60.0,
    ):
        """Initialize OKX funding rate feed.

        Args:
            symbols: List of perpetual swap symbols (e.g., ['BTC-USDT-SWAP'])
            timeframe: OHLCV bar timeframe ('1m', '1H', '4H', '1D')
            poll_interval_seconds: How often to poll for new data
        """
        self.symbols = symbols
        self.timeframe = timeframe
        self.poll_interval = poll_interval_seconds

        # State
        self._queue: asyncio.Queue = asyncio.Queue()
        self._running = False
        self._poll_task: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None

        # Track last emitted timestamp per symbol to avoid duplicates
        self._last_timestamps: dict[str, datetime | None] = {s: None for s in symbols}

        # Statistics
        self._bar_count = 0
        self._funding_updates = 0

    async def start(self) -> None:
        """Start the OKX data feed.

        Begins polling for OHLCV and funding rate data.
        """
        logger.info(f"OKXFundingFeed: Starting feed for {len(self.symbols)} symbols")
        self._running = True

        # Create async HTTP client
        self._client = httpx.AsyncClient(timeout=30.0)

        # Start polling task
        self._poll_task = asyncio.create_task(self._poll_loop())

        logger.info(f"OKXFundingFeed: Started polling every {self.poll_interval}s")

    def stop(self) -> None:
        """Stop the data feed."""
        logger.info("OKXFundingFeed: Stopping feed")
        self._running = False

        if self._poll_task:
            self._poll_task.cancel()

        # Signal consumer
        self._queue.put_nowait(None)

        logger.info(
            f"OKXFundingFeed: Stopped. Bars: {self._bar_count}, "
            f"Funding updates: {self._funding_updates}"
        )

    async def close(self) -> None:
        """Close HTTP client."""
        if self._client:
            await self._client.aclose()

    async def _poll_loop(self) -> None:
        """Main polling loop for market data."""
        try:
            while self._running:
                await self._fetch_and_emit()
                await asyncio.sleep(self.poll_interval)

        except asyncio.CancelledError:
            logger.info("OKXFundingFeed: Poll loop cancelled")
        except Exception as e:
            logger.error(f"OKXFundingFeed: Error in poll loop: {e}")

    async def _fetch_and_emit(self) -> None:
        """Fetch latest data and emit to queue."""
        try:
            # Fetch data for all symbols
            for symbol in self.symbols:
                # Get latest OHLCV bar
                ohlcv = await self._fetch_latest_ohlcv(symbol)
                if ohlcv is None:
                    continue

                timestamp, bar_data = ohlcv

                # Skip if we've already emitted this bar
                if self._last_timestamps[symbol] == timestamp:
                    continue

                # Get current funding rate
                funding_data = await self._fetch_funding_rate(symbol)

                # Build data dict
                data = {symbol: bar_data}

                # Build context with funding info
                context = {symbol: funding_data} if funding_data else {}

                # Emit to queue
                self._queue.put_nowait((timestamp, data, context))
                self._bar_count += 1
                self._last_timestamps[symbol] = timestamp

                logger.debug(
                    f"OKXFundingFeed: Emitted bar for {symbol} at {timestamp}, "
                    f"funding_rate={funding_data.get('funding_rate', 'N/A') if funding_data else 'N/A'}"
                )

        except Exception as e:
            logger.error(f"OKXFundingFeed: Error fetching data: {e}")

    async def _fetch_latest_ohlcv(self, symbol: str) -> tuple[datetime, dict] | None:
        """Fetch the most recent complete OHLCV bar.

        Args:
            symbol: OKX perpetual swap symbol

        Returns:
            Tuple of (timestamp, {open, high, low, close, volume}) or None
        """
        try:
            url = f"{self.BASE_URL}/market/candles"
            params = {
                "instId": symbol,
                "bar": self.timeframe,
                "limit": "2",  # Get last 2 to find the complete one
            }

            if self._client is None:
                return None

            response = await self._client.get(url, params=params)
            response.raise_for_status()
            result = response.json()

            if result.get("code") != "0":
                logger.warning(f"OKX API error: {result.get('msg')}")
                return None

            candles = result.get("data", [])
            if not candles:
                return None

            # OKX returns newest first, candle format:
            # [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
            # Use the second candle (index 1) which is complete
            # Index 0 is the current (incomplete) bar
            candle = candles[1] if len(candles) > 1 else candles[0]

            timestamp = datetime.fromtimestamp(int(candle[0]) / 1000, tz=UTC)
            bar_data = {
                "open": float(candle[1]),
                "high": float(candle[2]),
                "low": float(candle[3]),
                "close": float(candle[4]),
                "volume": float(candle[5]),
            }

            return timestamp, bar_data

        except Exception as e:
            logger.error(f"Error fetching OHLCV for {symbol}: {e}")
            return None

    async def _fetch_funding_rate(self, symbol: str) -> dict[str, Any] | None:
        """Fetch current funding rate for a symbol.

        Args:
            symbol: OKX perpetual swap symbol

        Returns:
            Dict with funding_rate, next_funding_rate, next_funding_time
        """
        try:
            if self._client is None:
                return None

            url = f"{self.BASE_URL}/public/funding-rate"
            params = {"instId": symbol}

            response = await self._client.get(url, params=params)
            response.raise_for_status()
            result = response.json()

            if result.get("code") != "0":
                logger.warning(f"OKX funding rate API error: {result.get('msg')}")
                return None

            data = result.get("data", [{}])[0]
            self._funding_updates += 1

            return {
                "funding_rate": float(data.get("fundingRate", 0)),
                "next_funding_rate": (
                    float(data["nextFundingRate"]) if data.get("nextFundingRate") else None
                ),
                "next_funding_time": (
                    datetime.fromtimestamp(int(data["nextFundingTime"]) / 1000, tz=UTC)
                    if data.get("nextFundingTime")
                    else None
                ),
            }

        except Exception as e:
            logger.error(f"Error fetching funding rate for {symbol}: {e}")
            return None

    def __aiter__(self):
        """Return async iterator."""
        return self

    async def __anext__(self) -> tuple[datetime, dict[str, Any], dict[str, Any]]:
        """Get next bar with funding data.

        Returns:
            (timestamp, data, context) tuple

        Raises:
            StopAsyncIteration: When feed stops
        """
        item = await self._queue.get()

        if item is None:  # Shutdown sentinel
            raise StopAsyncIteration

        return item

    @property
    def stats(self) -> dict[str, Any]:
        """Get feed statistics."""
        return {
            "running": self._running,
            "symbols": self.symbols,
            "timeframe": self.timeframe,
            "bar_count": self._bar_count,
            "funding_updates": self._funding_updates,
            "poll_interval": self.poll_interval,
        }
