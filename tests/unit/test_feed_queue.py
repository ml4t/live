"""Tests for bounded fail-closed feed buffering."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from ml4t.specs import (
    EventCompletion,
    LifecycleVersion,
    MarketEvent,
    MarketEventKind,
    TradePayload,
)

from ml4t.live.feeds.queue import BoundedEventQueue, FeedOverflowError


def trade(sequence: int, *, receipt_time: datetime | None = None) -> MarketEvent:
    timestamp = receipt_time or datetime.now(UTC)
    return MarketEvent(
        version=LifecycleVersion.V1,
        event_time=timestamp,
        receipt_time=timestamp,
        kind=MarketEventKind.TRADE,
        completion=EventCompletion.COMPLETE,
        source="fixture",
        asset="AAPL",
        payload=TradePayload(150.0 + sequence, 1.0),
        provider_sequence=sequence,
    )


@pytest.mark.asyncio
async def test_overflow_is_bounded_and_fails_before_draining_pending_events() -> None:
    queue = BoundedEventQueue(capacity=2, feed="fixture")
    queue.put_nowait(trade(1))
    queue.put_nowait(trade(2))

    with pytest.raises(FeedOverflowError) as raised:
        queue.put_nowait(trade(3))

    assert queue.qsize() == 0
    assert raised.value.gap.detected is True
    assert raised.value.gap.previous_sequence == 2
    assert raised.value.gap.current_sequence == 3
    assert raised.value.snapshot.capacity == 2
    assert raised.value.snapshot.occupancy == 2
    assert raised.value.snapshot.high_watermark == 2
    assert raised.value.snapshot.overflow_count == 1
    assert raised.value.snapshot.failed is True
    assert raised.value.snapshot.finished is True
    with pytest.raises(FeedOverflowError):
        await queue.get()


@pytest.mark.asyncio
async def test_waiting_consumer_is_woken_by_external_failure() -> None:
    queue = BoundedEventQueue(capacity=2, feed="fixture")
    consumer = asyncio.create_task(queue.get())
    await asyncio.sleep(0)

    queue.fail(RuntimeError("provider failed"), discard=True)

    with pytest.raises(RuntimeError, match="provider failed"):
        await consumer


@pytest.mark.asyncio
async def test_graceful_finish_drains_retained_events_in_order() -> None:
    queue = BoundedEventQueue(capacity=2, feed="fixture")
    first = trade(1)
    second = trade(2)
    queue.put_nowait(first)
    queue.put_nowait(second)
    queue.finish(discard=False)

    assert await queue.get() is first
    assert await queue.get() is second
    assert await queue.get() is None


def test_queue_snapshot_reports_capacity_occupancy_and_lag() -> None:
    now = datetime.now(UTC)
    queue = BoundedEventQueue(capacity=4, feed="fixture")
    queue.put_nowait(trade(1, receipt_time=now - timedelta(seconds=3)))

    snapshot = queue.snapshot(now=now)

    assert snapshot.capacity == 4
    assert snapshot.occupancy == 1
    assert snapshot.high_watermark == 1
    assert snapshot.oldest_event_lag_seconds == 3.0
    assert snapshot.failed is False
    assert snapshot.finished is False


@pytest.mark.parametrize("capacity", [True, 0, -1, 1.5])
def test_invalid_capacity_is_rejected_before_state_exists(capacity: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        BoundedEventQueue(capacity=capacity, feed="fixture")
