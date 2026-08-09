"""Sustained bounded-memory workload for the supported feed queue contract."""

import gc
from datetime import UTC, datetime

import psutil
from ml4t.specs import (
    BarPayload,
    EventCompletion,
    FundingPayload,
    LifecycleVersion,
    MarketEvent,
    MarketEventKind,
    QuotePayload,
    TradePayload,
)

from ml4t.live.feeds.queue import BoundedEventQueue, FeedOverflowError


def event(kind: MarketEventKind, sequence: int) -> MarketEvent:
    timestamp = datetime.now(UTC)
    payload = {
        MarketEventKind.BAR: BarPayload(149.0, 151.0, 148.0, 150.0, 100.0),
        MarketEventKind.QUOTE: QuotePayload(149.0, 151.0, 10.0, 20.0),
        MarketEventKind.TRADE: TradePayload(150.0, 1.0),
        MarketEventKind.FUNDING: FundingPayload(0.0001),
    }[kind]
    return MarketEvent(
        version=LifecycleVersion.V1,
        event_time=timestamp,
        receipt_time=timestamp,
        kind=kind,
        completion=(
            EventCompletion.EVOLVING
            if kind in {MarketEventKind.QUOTE, MarketEventKind.TRADE}
            else EventCompletion.COMPLETE
        ),
        source="stress",
        asset=f"ASSET-{sequence % 32}",
        payload=payload,
        provider_sequence=sequence,
    )


def test_sustained_overload_keeps_every_event_kind_and_rss_bounded() -> None:
    process = psutil.Process()
    gc.collect()
    baseline_rss = process.memory_info().rss

    for kind in MarketEventKind:
        queue = BoundedEventQueue(capacity=64, feed=f"stress-{kind.value}")
        for sequence in range(25_000):
            try:
                queue.put_nowait(event(kind, sequence))
            except FeedOverflowError:
                pass

        snapshot = queue.snapshot()
        assert snapshot.capacity == 64
        assert snapshot.occupancy == 0
        assert snapshot.high_watermark == 64
        assert snapshot.overflow_count == 1
        assert snapshot.failed is True

    gc.collect()
    rss_growth = process.memory_info().rss - baseline_rss
    assert rss_growth < 20 * 1024 * 1024
