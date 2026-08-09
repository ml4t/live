"""Bounded fail-closed buffering for supported live feeds."""

from __future__ import annotations

import asyncio
import math
from collections import deque
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any

from ml4t.specs import GapEvidence, MarketEvent


@dataclass(frozen=True, slots=True)
class FeedQueueSnapshot:
    """Observable state for one supported feed queue."""

    capacity: int
    occupancy: int
    high_watermark: int
    overflow_count: int
    oldest_event_lag_seconds: float | None
    failed: bool
    finished: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FeedOverflowError(RuntimeError):
    """Raised when continuing after a full queue would hide lost market data."""

    def __init__(
        self,
        *,
        feed: str,
        event: MarketEvent,
        snapshot: FeedQueueSnapshot,
    ) -> None:
        self.feed = feed
        self.asset = event.asset
        self.kind = event.kind.value
        self.snapshot = snapshot
        self.gap = GapEvidence(
            True,
            f"{feed} queue overflow rejected {event.kind.value} for {event.asset}",
            previous_sequence=f"retained:{feed}",
            current_sequence=f"rejected:{event.source}:{event.asset}",
        )
        super().__init__(
            f"{feed} queue capacity {snapshot.capacity} exceeded by "
            f"{event.kind.value} event for {event.asset}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "feed": self.feed,
            "asset": self.asset,
            "kind": self.kind,
            "gap": asdict(self.gap),
            "queue": self.snapshot.to_dict(),
        }


class BoundedEventQueue:
    """Event-loop queue that halts instead of silently dropping market data."""

    def __init__(self, *, capacity: int, feed: str) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int):
            raise TypeError("queue capacity must be an integer")
        if capacity <= 0:
            raise ValueError("queue capacity must be positive")
        if not isinstance(feed, str) or not feed.strip():
            raise ValueError("feed must be a non-empty string")
        self.capacity = capacity
        self.feed = feed
        self._items: deque[MarketEvent] = deque()
        self._changed = asyncio.Event()
        self._failure: Exception | None = None
        self._finished = False
        self._high_watermark = 0
        self._overflow_count = 0

    def put_nowait(self, item: MarketEvent | None) -> None:
        if item is None:
            self.finish(discard=False)
            return
        if not isinstance(item, MarketEvent):
            raise TypeError("supported feed queues accept MarketEvent objects only")
        if self._failure is not None:
            raise self._failure
        if self._finished:
            raise RuntimeError(f"{self.feed} queue is finished")
        if len(self._items) >= self.capacity:
            self._overflow_count += 1
            snapshot = replace(self.snapshot(), failed=True, finished=True)
            error = FeedOverflowError(feed=self.feed, event=item, snapshot=snapshot)
            error.gap = self._overflow_gap(self._items[-1], item)
            self.fail(error, discard=True)
            raise error
        self._items.append(item)
        self._high_watermark = max(self._high_watermark, len(self._items))
        self._changed.set()

    async def put(self, item: MarketEvent | None) -> None:
        self.put_nowait(item)

    def _overflow_gap(self, previous: MarketEvent, current: MarketEvent) -> GapEvidence:
        previous_sequence = previous.provider_sequence
        current_sequence = current.provider_sequence
        valid_provider_pair = (
            previous_sequence is not None
            and current_sequence is not None
            and type(previous_sequence) is type(current_sequence)
            and previous_sequence != current_sequence
            and not (
                isinstance(previous_sequence, int)
                and isinstance(current_sequence, int)
                and previous_sequence >= current_sequence
            )
        )
        if not valid_provider_pair:
            previous_sequence = f"retained:{previous.source}:{previous.asset}"
            current_sequence = f"rejected:{current.source}:{current.asset}"
        return GapEvidence(
            True,
            f"{self.feed} queue overflow rejected {current.kind.value} for {current.asset}",
            previous_sequence=previous_sequence,
            current_sequence=current_sequence,
        )

    async def get(self) -> MarketEvent | None:
        while True:
            if self._failure is not None:
                raise self._failure
            if self._items:
                return self._items.popleft()
            if self._finished:
                return None
            self._changed.clear()
            if self._failure is not None or self._items or self._finished:
                continue
            await self._changed.wait()

    def get_nowait(self) -> MarketEvent | None:
        if self._failure is not None:
            raise self._failure
        if self._items:
            return self._items.popleft()
        if self._finished:
            return None
        raise asyncio.QueueEmpty

    def finish(self, *, discard: bool) -> None:
        if discard:
            self._items.clear()
        self._finished = True
        self._changed.set()

    def fail(self, error: Exception, *, discard: bool) -> None:
        if self._failure is None:
            self._failure = error
        if discard:
            self._items.clear()
        self._finished = True
        self._changed.set()

    def qsize(self) -> int:
        return len(self._items)

    def empty(self) -> bool:
        return not self._items

    def full(self) -> bool:
        return len(self._items) >= self.capacity

    def snapshot(self, *, now: datetime | None = None) -> FeedQueueSnapshot:
        now = now or datetime.now(UTC)
        if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
            raise ValueError("now must be timezone-aware UTC")
        lag = None
        if self._items:
            lag = max(0.0, (now - self._items[0].receipt_time).total_seconds())
            if not math.isfinite(lag):
                lag = None
        return FeedQueueSnapshot(
            capacity=self.capacity,
            occupancy=len(self._items),
            high_watermark=self._high_watermark,
            overflow_count=self._overflow_count,
            oldest_event_lag_seconds=lag,
            failed=self._failure is not None,
            finished=self._finished,
        )


__all__ = ["BoundedEventQueue", "FeedOverflowError", "FeedQueueSnapshot"]
