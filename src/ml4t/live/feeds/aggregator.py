"""Bar aggregation for live market data feeds.

This module provides tools for accumulating ticks and sub-minute bars into minute bars
for strategy consumption. BarBuffer handles the OHLCV aggregation logic.
"""

import asyncio
import logging
import math
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ml4t.specs import (
    BarPayload,
    EventCompletion,
    GapEvidence,
    LifecycleVersion,
    MarketEvent,
    MarketEventKind,
    TradePayload,
)

from ml4t.live.feeds.events import (
    FeedContractError,
    sequence_unavailable,
    strategy_input,
    utc_datetime,
    validate_event_timing,
)
from ml4t.live.feeds.queue import BoundedEventQueue

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
    high: float = float("-inf")
    low: float = float("inf")
    close: float = 0.0
    volume: float = 0.0

    def update(self, price: float, size: float = 0) -> None:
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

    def update_bar(self, payload: dict[str, Any]) -> None:
        """Merge one validated OHLCV payload without discarding its range."""
        open_price = float(payload["open"])
        high = float(payload["high"])
        low = float(payload["low"])
        close = float(payload["close"])
        if self.open is None:
            self.open = open_price
        self.high = max(self.high, high)
        self.low = min(self.low, low)
        self.close = close
        self.volume += float(payload.get("volume", 0.0))

    def to_dict(self) -> dict[str, Any]:
        """Convert to OHLCV dict.

        Returns:
            Dictionary with keys: open, high, low, close, volume
            If no ticks received, uses close price as fallback for OHLC
        """
        return {
            "open": self.open or self.close,
            "high": self.high if self.high != float("-inf") else self.close,
            "low": self.low if self.low != float("inf") else self.close,
            "close": self.close,
            "volume": self.volume,
        }

    def reset(self) -> None:
        """Reset for next bar."""
        self.open = None
        self.high = float("-inf")
        self.low = float("inf")
        self.close = 0.0
        self.volume = 0.0


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

        async for event in aggregated_feed:
            consume(event)
    """

    def __init__(
        self,
        source_feed: "DataFeedProtocol",
        bar_size_minutes: int = 1,
        assets: list[str] | None = None,
        flush_timeout_seconds: float = 2.0,
        queue_capacity: int = 256,
    ):
        """Initialize BarAggregator.

        Args:
            source_feed: Raw tick or sub-minute bar feed
            bar_size_minutes: Output bar size in minutes (default: 1)
            assets: List of assets to track (default: all from source)
            flush_timeout_seconds: Seconds after bar end before forcing emit (default: 2.0)
            queue_capacity: Maximum pending bars before a fail-closed overflow.
        """
        if isinstance(bar_size_minutes, bool) or not isinstance(bar_size_minutes, int):
            raise TypeError("bar_size_minutes must be an integer")
        if bar_size_minutes <= 0:
            raise ValueError("bar_size_minutes must be positive")
        if (
            isinstance(flush_timeout_seconds, bool)
            or not isinstance(flush_timeout_seconds, int | float)
            or not math.isfinite(flush_timeout_seconds)
            or flush_timeout_seconds < 0
        ):
            raise ValueError("flush_timeout_seconds must be finite and non-negative")
        self.source = source_feed
        self.bar_size = timedelta(minutes=bar_size_minutes)
        self.assets = assets or []
        self.flush_timeout = flush_timeout_seconds
        self.queue_capacity = queue_capacity

        # Per-asset bar buffers
        self._buffers: dict[str, BarBuffer] = {}
        self._current_bar_start: dict[str, datetime] = {}
        self._source_events: dict[str, MarketEvent] = {}
        self._last_completed_bar: dict[str, datetime] = {}
        self._last_data_time: float = 0  # Track when we last got data

        # Output queue (use None sentinel for shutdown instead of timeout)
        self._queue = BoundedEventQueue(capacity=queue_capacity, feed="bar_aggregator")
        self._running = False
        self._aggregate_task: asyncio.Task | None = None
        self._flush_task: asyncio.Task | None = None
        self._failure: Exception | None = None

    async def start(self) -> None:
        """Start aggregation."""
        if self._running:
            return
        self._failure = None
        self._queue = BoundedEventQueue(capacity=self.queue_capacity, feed="bar_aggregator")
        self._running = True
        await self.source.start()

        # Start aggregation task
        self._aggregate_task = asyncio.create_task(self._aggregate_loop())

    def stop(self) -> None:
        """Stop aggregation."""
        self._running = False
        self.source.stop()
        if self._flush_task:
            self._flush_task.cancel()
        if self._aggregate_task is None:
            self._queue.finish(discard=True)

    async def _aggregate_loop(self) -> None:
        """Main aggregation loop."""
        # Start background flush checker (Gemini "stuck bar" fix)
        self._flush_task = asyncio.create_task(self._flush_checker())

        try:
            async for item in self.source:
                if not self._running:
                    break

                if isinstance(item, MarketEvent):
                    if item.kind is MarketEventKind.FUNDING:
                        continue
                    processing_time = datetime.now(UTC)
                    validate_event_timing(
                        item,
                        processing_time=processing_time,
                        max_age_seconds=None,
                    )
                    timestamp, data, _context = strategy_input(
                        item,
                        processing_time=processing_time,
                    )
                    self._source_events[item.asset] = item
                else:
                    timestamp, data, _context = item
                    timestamp = utc_datetime(timestamp, "aggregator input timestamp")

                # Track when we got data (for flush timeout)
                self._last_data_time = time.time()

                # Accumulate data into buffers
                for asset, ohlcv in data.items():
                    if self.assets and asset not in self.assets:
                        continue

                    bar_start = self._truncate_to_bar(timestamp)
                    if bar_start <= self._last_completed_bar.get(
                        asset, datetime.min.replace(tzinfo=UTC)
                    ):
                        logger.warning("BarAggregator: Rejected event for completed %s bar", asset)
                        continue
                    current_start = self._current_bar_start.get(asset)
                    if current_start is not None and bar_start > current_start:
                        await self._emit_asset_bar(asset, current_start)
                    elif current_start is not None and bar_start < current_start:
                        logger.warning("BarAggregator: Rejected late event for %s", asset)
                        continue
                    self._current_bar_start[asset] = bar_start

                    if asset not in self._buffers:
                        self._buffers[asset] = BarBuffer()

                    # Handle different data formats
                    if all(field in ohlcv for field in ("open", "high", "low", "close")):
                        # OHLCV bar data
                        payload = BarPayload(
                            open=ohlcv["open"],
                            high=ohlcv["high"],
                            low=ohlcv["low"],
                            close=ohlcv["close"],
                            volume=ohlcv.get("volume", 0.0),
                        )
                        self._buffers[asset].update_bar(
                            {
                                "open": payload.open,
                                "high": payload.high,
                                "low": payload.low,
                                "close": payload.close,
                                "volume": payload.volume,
                            }
                        )
                    elif "price" in ohlcv:
                        # Tick data
                        payload = TradePayload(
                            price=ohlcv["price"],
                            size=ohlcv.get("size", 0.0),
                        )
                        self._buffers[asset].update(payload.price, payload.size)
                    else:
                        raise FeedContractError(
                            f"aggregator input for {asset!r} must contain OHLC or price fields"
                        )
        except Exception as exc:
            self._failure = exc
            logger.exception("Bar aggregation failed")
        finally:
            if self._flush_task:
                self._flush_task.cancel()
                self._flush_task = None
            if not self._queue.snapshot().failed:
                for asset, bar_start in tuple(self._current_bar_start.items()):
                    completion = (
                        EventCompletion.COMPLETE
                        if datetime.now(UTC) >= bar_start + self.bar_size
                        else EventCompletion.EVOLVING
                    )
                    await self._emit_asset_bar(asset, bar_start, completion=completion)
            self._current_bar_start.clear()
            self._running = False
            self._aggregate_task = None
            self._signal_stop()

    async def _flush_checker(self) -> None:
        """Force emit bars if no data arrives (Gemini "stuck bar" fix).

        Scenario: Market closes at 16:00, last tick at 15:59:58. Without this,
        the 15:59 bar never emits because no 16:00 tick arrives to trigger it.
        """
        while self._running:
            await asyncio.sleep(1.0)
            if not self._current_bar_start:
                continue
            for asset, bar_start in tuple(self._current_bar_start.items()):
                now = datetime.now(tz=bar_start.tzinfo)
                bar_end = bar_start + self.bar_size
                if now > bar_end + timedelta(seconds=self.flush_timeout):
                    logger.debug("Flush: Emitting stale %s bar at %s", asset, bar_start)
                    await self._emit_asset_bar(asset, bar_start)
                    self._current_bar_start.pop(asset, None)

    def _signal_stop(self) -> None:
        """Signal consumers that iteration should stop."""
        self._queue.finish(discard=False)

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

    async def _emit_asset_bar(
        self,
        asset: str,
        bar_time: datetime,
        *,
        completion: EventCompletion = EventCompletion.COMPLETE,
    ) -> None:
        """Emit completed bar.

        Args:
            bar_time: Timestamp for the bar being emitted
        """
        buffer = self._buffers.get(asset)
        if buffer is None or buffer.open is None:
            return
        payload = BarPayload(**buffer.to_dict())
        buffer.reset()
        source_event = self._source_events.get(asset)
        gap: GapEvidence = (
            source_event.gap
            if source_event is not None and source_event.gap is not None
            else sequence_unavailable("BarAggregator", "multiple input events per bar")
        )
        event = MarketEvent(
            version=LifecycleVersion.V1,
            event_time=bar_time,
            receipt_time=datetime.now(UTC),
            kind=MarketEventKind.BAR,
            completion=completion,
            source="bar_aggregator",
            asset=asset,
            payload=payload,
            gap=gap,
            metadata={
                "bar_start": bar_time.isoformat(),
                "bar_end": (bar_time + self.bar_size).isoformat(),
                "source": source_event.source if source_event is not None else "legacy",
            },
        )
        await self._queue.put(event)
        if completion is EventCompletion.COMPLETE:
            self._last_completed_bar[asset] = bar_time
        logger.debug("Emitted completed bar at %s: %s", bar_time, asset)

    async def _emit_bar(self, bar_time: datetime) -> None:
        """Emit all populated assets for compatibility with the public helper."""
        for asset in tuple(self._buffers):
            await self._emit_asset_bar(asset, bar_time)

    async def __aiter__(self) -> AsyncIterator[MarketEvent]:
        """Async iterator interface.

        Uses None sentinel for shutdown (Gemini fix: avoids busy-wait with 1s timeout).

        Yields:
            One validated bar event per asset and interval.
        """
        while True:
            item = await self._queue.get()
            if item is None:  # Shutdown sentinel
                if self._failure is not None:
                    raise self._failure
                break
            yield item

    @property
    def stats(self) -> dict[str, Any]:
        """Return aggregation and bounded-queue state."""
        return {
            "running": self._running,
            "bar_size_seconds": self.bar_size.total_seconds(),
            "tracked_assets": sorted(self._current_bar_start),
            "completed_assets": sorted(self._last_completed_bar),
            "queue": self._queue.snapshot().to_dict(),
        }
