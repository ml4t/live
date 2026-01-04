"""Bar aggregation for live market data feeds.

This module provides tools for accumulating ticks and sub-minute bars into minute bars
for strategy consumption. BarBuffer handles the OHLCV aggregation logic.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, AsyncIterator, Any

if TYPE_CHECKING:
    from ml4t.live.protocols import DataFeedProtocol

logger = logging.getLogger(__name__)


@dataclass
class BarBuffer:
    """Accumulates ticks into OHLCV bar.

    Attributes:
        open: Opening price (first tick)
        high: Highest price seen
        low: Lowest price seen
        close: Most recent price
        volume: Total volume accumulated
    """

    open: float | None = None
    high: float = float('-inf')
    low: float = float('inf')
    close: float = 0.0
    volume: int = 0

    def update(self, price: float, size: int = 0) -> None:
        """Add a tick to the bar.

        Args:
            price: Trade price
            size: Trade size (defaults to 0 for quote ticks)
        """
        if self.open is None:
            self.open = price
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += size

    def to_dict(self) -> dict[str, Any]:
        """Convert to OHLCV dict.

        Returns:
            Dictionary with keys: open, high, low, close, volume
            If no ticks received, uses close price as fallback for OHLC
        """
        return {
            'open': self.open or self.close,
            'high': self.high if self.high != float('-inf') else self.close,
            'low': self.low if self.low != float('inf') else self.close,
            'close': self.close,
            'volume': self.volume,
        }

    def reset(self) -> None:
        """Reset for next bar."""
        self.open = None
        self.high = float('-inf')
        self.low = float('inf')
        self.close = 0.0
        self.volume = 0


class BarAggregator:
    """Aggregates raw ticks or 5-second bars into minute bars.

    Addresses Gemini's concerns:
    1. "If IBDataFeed pushes a tick to Strategy.on_data, the strategy might
       trigger 60x more often than intended." - Buffer incoming data.
    2. "The 15:59 bar is never emitted because no 16:00 tick arrives." -
       Background flush checker emits bars on timeout.

    The aggregator buffers incoming data and emits when:
    - A bar boundary is crossed (new tick arrives in next minute)
    - OR timeout expires (2s past bar end with no new data)

    Example:
        raw_feed = IBTickFeed(ib, assets=['AAPL'])
        aggregated_feed = BarAggregator(raw_feed, bar_size_minutes=1)

        async for timestamp, data, context in aggregated_feed:
            # data contains completed minute bars only
            strategy.on_data(timestamp, data, context, broker)
    """

    def __init__(
        self,
        source_feed: "DataFeedProtocol",
        bar_size_minutes: int = 1,
        assets: list[str] | None = None,
        flush_timeout_seconds: float = 2.0,
    ):
        """Initialize BarAggregator.

        Args:
            source_feed: Raw tick or sub-minute bar feed
            bar_size_minutes: Output bar size in minutes (default: 1)
            assets: List of assets to track (default: all from source)
            flush_timeout_seconds: Seconds after bar end before forcing emit (default: 2.0)
        """
        self.source = source_feed
        self.bar_size = timedelta(minutes=bar_size_minutes)
        self.assets = assets or []
        self.flush_timeout = flush_timeout_seconds

        # Per-asset bar buffers
        self._buffers: dict[str, BarBuffer] = {}
        self._current_bar_start: datetime | None = None
        self._last_data_time: float = 0  # Track when we last got data

        # Output queue (use None sentinel for shutdown instead of timeout)
        self._queue: asyncio.Queue = asyncio.Queue()
        self._running = False
        self._flush_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start aggregation."""
        self._running = True
        await self.source.start()

        # Start aggregation task
        asyncio.create_task(self._aggregate_loop())

    def stop(self) -> None:
        """Stop aggregation."""
        self._running = False
        self.source.stop()
        # Signal consumer to exit via sentinel
        self._queue.put_nowait(None)
        # Cancel flush task
        if self._flush_task:
            self._flush_task.cancel()

    async def _aggregate_loop(self) -> None:
        """Main aggregation loop."""
        # Start background flush checker (Gemini "stuck bar" fix)
        self._flush_task = asyncio.create_task(self._flush_checker())

        try:
            async for timestamp, data, context in self.source:
                if not self._running:
                    break

                # Track when we got data (for flush timeout)
                self._last_data_time = time.time()

                # Determine bar boundary
                bar_start = self._truncate_to_bar(timestamp)

                # If bar boundary crossed, emit completed bar
                if self._current_bar_start and bar_start > self._current_bar_start:
                    await self._emit_bar(self._current_bar_start)

                # Update current bar start
                self._current_bar_start = bar_start

                # Accumulate data into buffers
                for asset, ohlcv in data.items():
                    if asset not in self._buffers:
                        self._buffers[asset] = BarBuffer()

                    # Handle different data formats
                    if 'close' in ohlcv:
                        # OHLCV bar data
                        self._buffers[asset].update(ohlcv['close'], ohlcv.get('volume', 0))
                    elif 'price' in ohlcv:
                        # Tick data
                        self._buffers[asset].update(ohlcv['price'], ohlcv.get('size', 0))
        finally:
            if self._flush_task:
                self._flush_task.cancel()

    async def _flush_checker(self) -> None:
        """Force emit bars if no data arrives (Gemini "stuck bar" fix).

        Scenario: Market closes at 16:00, last tick at 15:59:58. Without this,
        the 15:59 bar never emits because no 16:00 tick arrives to trigger it.
        """
        while self._running:
            await asyncio.sleep(1.0)
            if not self._current_bar_start:
                continue

            # Check if we're past the bar end time + flush timeout
            now = datetime.now()
            bar_end = self._current_bar_start + self.bar_size

            if now > bar_end + timedelta(seconds=self.flush_timeout):
                logger.debug(f"Flush: Emitting stale bar at {self._current_bar_start}")
                await self._emit_bar(self._current_bar_start)
                self._current_bar_start = None  # Prevent double emit

    def _truncate_to_bar(self, dt: datetime) -> datetime:
        """Truncate datetime to bar boundary.

        Args:
            dt: Datetime to truncate

        Returns:
            Datetime truncated to bar boundary (e.g., 10:35:42 -> 10:35:00 for 1-min bars)
        """
        minutes_per_bar = int(self.bar_size.total_seconds() // 60)
        truncated_minutes = (dt.minute // minutes_per_bar) * minutes_per_bar
        return dt.replace(minute=truncated_minutes, second=0, microsecond=0)

    async def _emit_bar(self, bar_time: datetime) -> None:
        """Emit completed bar.

        Args:
            bar_time: Timestamp for the bar being emitted
        """
        if not self._buffers:
            return

        # Build bar data
        data = {}
        for asset, buffer in self._buffers.items():
            if buffer.open is not None:  # Has data
                data[asset] = buffer.to_dict()
                buffer.reset()

        if data:
            await self._queue.put((bar_time, data, {}))
            logger.debug(f"Emitted bar at {bar_time}: {list(data.keys())}")

    async def __aiter__(self) -> AsyncIterator[tuple[datetime, dict, dict]]:
        """Async iterator interface.

        Uses None sentinel for shutdown (Gemini fix: avoids busy-wait with 1s timeout).

        Yields:
            Tuple of (timestamp, data, context) where data is {asset: ohlcv_dict}
        """
        while True:
            item = await self._queue.get()
            if item is None:  # Shutdown sentinel
                break
            yield item
