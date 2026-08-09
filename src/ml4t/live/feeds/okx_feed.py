"""OKX funding rate feed for crypto perpetuals.

Provides OHLCV bars plus funding rate data for perpetual swap contracts.
No API key required, no geo-restrictions.

Key Features:
- Hourly OHLCV bars for price data
- Funding rate updates (every 8 hours, polled hourly)
- Combined data + context for ML strategy consumption

Data Format:
    Separate validated bar and funding events.

Example:
    feed = OKXFundingFeed(
        symbols=['BTC-USDT-SWAP', 'ETH-USDT-SWAP'],
        timeframe='1H',
    )
    await feed.start()

    async for event in feed:
        consume(event)
"""

import asyncio
import logging
import math
from datetime import UTC, datetime
from typing import Any

import httpx
from ml4t.specs import (
    BarPayload,
    EventCompletion,
    FundingPayload,
    LifecycleVersion,
    MarketEvent,
    MarketEventKind,
)

from ml4t.live.feeds.events import sequence_unavailable
from ml4t.live.persistence import redact_sensitive
from ml4t.live.protocols import DataFeedProtocol

logger = logging.getLogger(__name__)


class OKXFundingFeed(DataFeedProtocol):
    """OKX funding rate feed with OHLCV bars.

    Combines price data with funding rate information for ML strategies
    that trade crypto perpetual futures based on funding rate signals.

    Data Flow:
        1. Poll /market/candles for latest OHLCV bar
        2. Poll /public/funding-rate for current funding rate
        3. Emit each causal record as its own event

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
        if not symbols or any(
            not isinstance(symbol, str) or not symbol.strip() for symbol in symbols
        ):
            raise ValueError("symbols must contain at least one non-empty symbol")
        if (
            isinstance(poll_interval_seconds, bool)
            or not isinstance(poll_interval_seconds, int | float)
            or not math.isfinite(poll_interval_seconds)
            or poll_interval_seconds <= 0
        ):
            raise ValueError("poll_interval_seconds must be finite and positive")
        self.symbols = list(symbols)
        self.timeframe = timeframe
        self.poll_interval = poll_interval_seconds

        # State
        self._queue: asyncio.Queue[MarketEvent | None] = asyncio.Queue()
        self._running = False
        self._poll_task: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None
        self._failure: Exception | None = None

        self._emitted_bars: set[tuple[str, datetime, EventCompletion]] = set()
        self._emitted_evolving: dict[tuple[str, datetime], BarPayload] = {}
        self._emitted_funding: set[tuple[str, str | int | None, float, str | None]] = set()
        self.max_event_age_seconds = self._timeframe_seconds(timeframe) * 2 + poll_interval_seconds

        # Statistics
        self._bar_count = 0
        self._funding_updates = 0
        self._rejected_count = 0
        self._error_count = 0

    async def start(self) -> None:
        """Start the OKX data feed.

        Begins polling for OHLCV and funding rate data.
        """
        logger.info(f"OKXFundingFeed: Starting feed for {len(self.symbols)} symbols")
        if self._running:
            return
        await self.close()
        self._queue = asyncio.Queue()
        self._failure = None
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
        if self._poll_task is not None:
            self._running = False
            if not self._poll_task.done():
                self._poll_task.cancel()
            if self._poll_task is not asyncio.current_task():
                await asyncio.gather(self._poll_task, return_exceptions=True)
            self._poll_task = None
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _poll_loop(self) -> None:
        """Main polling loop for market data."""
        try:
            while self._running:
                await self._fetch_and_emit()
                await asyncio.sleep(self.poll_interval)

        except asyncio.CancelledError:
            logger.info("OKXFundingFeed: Poll loop cancelled")
        except Exception as e:
            self._failure = e
            self._running = False
            self._queue.put_nowait(None)
            logger.error("OKXFundingFeed: Error in poll loop: %s", redact_sensitive(str(e)))

    async def _fetch_and_emit(self) -> None:
        """Fetch latest data and emit to queue."""
        try:
            # Fetch data for all symbols
            for symbol in self.symbols:
                # Get latest OHLCV bar
                ohlcv = await self._fetch_latest_ohlcv(symbol)
                if ohlcv is not None:
                    bar_key = (symbol, ohlcv.event_time, ohlcv.completion)
                    evolving_key = (symbol, ohlcv.event_time)
                    should_emit = bar_key not in self._emitted_bars
                    if ohlcv.completion is EventCompletion.EVOLVING:
                        should_emit = self._emitted_evolving.get(evolving_key) != ohlcv.payload
                    if should_emit:
                        self._queue.put_nowait(ohlcv)
                        self._bar_count += 1
                        if ohlcv.completion is EventCompletion.COMPLETE:
                            self._emitted_bars.add(bar_key)
                            self._emitted_evolving.pop(evolving_key, None)
                        else:
                            assert isinstance(ohlcv.payload, BarPayload)
                            self._emitted_evolving[evolving_key] = ohlcv.payload

                funding_event = await self._fetch_funding_rate(symbol)
                if funding_event is not None:
                    assert isinstance(funding_event.payload, FundingPayload)
                    funding_key = (
                        symbol,
                        funding_event.provider_sequence,
                        funding_event.payload.rate,
                        funding_event.metadata.get("next_funding_time"),
                    )
                    if funding_key not in self._emitted_funding:
                        self._queue.put_nowait(funding_event)
                        self._emitted_funding.add(funding_key)

                if ohlcv is not None:
                    logger.debug(
                        "OKXFundingFeed: Processed %s bar for %s at %s",
                        ohlcv.completion.value,
                        symbol,
                        ohlcv.event_time,
                    )

        except Exception as e:
            self._error_count += 1
            logger.error("OKXFundingFeed: Error fetching data: %s", redact_sensitive(str(e)))

    async def _fetch_latest_ohlcv(self, symbol: str) -> MarketEvent | None:
        """Fetch the most recent complete OHLCV bar.

        Args:
            symbol: OKX perpetual swap symbol

        Returns:
            A validated bar event or ``None`` when the endpoint has no usable record.
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
                self._error_count += 1
                logger.warning(f"OKX API error: {result.get('msg')}")
                return None

            candles = result.get("data", [])
            if not candles:
                return None

            complete = [candle for candle in candles if len(candle) > 8 and candle[8] == "1"]
            candle = complete[0] if complete else candles[0]

            timestamp = datetime.fromtimestamp(int(candle[0]) / 1000, tz=UTC)
            completion = (
                EventCompletion.COMPLETE
                if len(candle) > 8 and candle[8] == "1"
                else EventCompletion.EVOLVING
            )
            return MarketEvent(
                version=LifecycleVersion.V1,
                event_time=timestamp,
                receipt_time=datetime.now(UTC),
                kind=MarketEventKind.BAR,
                completion=completion,
                source="okx",
                asset=symbol,
                payload=BarPayload(
                    float(candle[1]),
                    float(candle[2]),
                    float(candle[3]),
                    float(candle[4]),
                    float(candle[5]),
                ),
                provider_sequence=int(candle[0]),
                metadata={"timeframe": self.timeframe},
            )

        except (IndexError, KeyError, TypeError, ValueError) as e:
            self._rejected_count += 1
            logger.warning(
                "Rejected OHLCV payload for %s: %s",
                symbol,
                redact_sensitive(str(e)),
            )
            return None
        except Exception as e:
            self._error_count += 1
            logger.error("Error fetching OHLCV for %s: %s", symbol, redact_sensitive(str(e)))
            return None

    async def _fetch_funding_rate(self, symbol: str) -> MarketEvent | None:
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
                self._error_count += 1
                logger.warning(f"OKX funding rate API error: {result.get('msg')}")
                return None

            data = result.get("data", [{}])[0]
            receipt_time = datetime.now(UTC)
            funding_time = data.get("fundingTime")
            event = MarketEvent(
                version=LifecycleVersion.V1,
                event_time=receipt_time,
                receipt_time=receipt_time,
                kind=MarketEventKind.FUNDING,
                completion=EventCompletion.COMPLETE,
                source="okx",
                asset=symbol,
                payload=FundingPayload(float(data["fundingRate"])),
                provider_sequence=int(funding_time) if funding_time else None,
                gap=(
                    sequence_unavailable("OKX", "fundingTime missing") if not funding_time else None
                ),
                metadata={
                    "funding_time": (
                        datetime.fromtimestamp(int(funding_time) / 1000, tz=UTC).isoformat()
                        if funding_time
                        else None
                    ),
                    "next_funding_rate": (
                        float(data["nextFundingRate"]) if data.get("nextFundingRate") else None
                    ),
                    "next_funding_time": (
                        datetime.fromtimestamp(
                            int(data["nextFundingTime"]) / 1000,
                            tz=UTC,
                        ).isoformat()
                        if data.get("nextFundingTime")
                        else None
                    ),
                },
            )
            self._funding_updates += 1
            return event

        except (IndexError, KeyError, TypeError, ValueError) as e:
            self._rejected_count += 1
            logger.warning(
                "Rejected funding payload for %s: %s",
                symbol,
                redact_sensitive(str(e)),
            )
            return None
        except Exception as e:
            self._error_count += 1
            logger.error(
                "Error fetching funding rate for %s: %s",
                symbol,
                redact_sensitive(str(e)),
            )
            return None

    def __aiter__(self):
        """Return async iterator."""
        return self

    async def __anext__(self) -> MarketEvent:
        """Get next bar with funding data.

        Returns:
            A validated bar or funding event.

        Raises:
            StopAsyncIteration: When feed stops
        """
        item = await self._queue.get()

        if item is None:  # Shutdown sentinel
            if self._failure is not None:
                raise RuntimeError("OKX polling failed") from self._failure
            raise StopAsyncIteration

        return item

    @staticmethod
    def _timeframe_seconds(timeframe: str) -> float:
        units = {"m": 60, "H": 3_600, "D": 86_400}
        if len(timeframe) < 2 or timeframe[-1] not in units:
            raise ValueError("timeframe must end in m, H, or D")
        try:
            count = int(timeframe[:-1])
        except ValueError as error:
            raise ValueError("timeframe must begin with a positive integer") from error
        if count <= 0:
            raise ValueError("timeframe must begin with a positive integer")
        return float(count * units[timeframe[-1]])

    @property
    def stats(self) -> dict[str, Any]:
        """Get feed statistics."""
        return {
            "running": self._running,
            "symbols": self.symbols,
            "timeframe": self.timeframe,
            "bar_count": self._bar_count,
            "funding_updates": self._funding_updates,
            "rejected_count": self._rejected_count,
            "error_count": self._error_count,
            "poll_interval": self.poll_interval,
        }
