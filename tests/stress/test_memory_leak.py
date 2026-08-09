"""Sustained memory checks for retained causal feed state."""

import gc
from datetime import UTC, datetime, timedelta

import psutil
import pytest
from ml4t.specs import (
    BarPayload,
    EventCompletion,
    LifecycleVersion,
    MarketEvent,
    MarketEventKind,
)

from ml4t.live.feeds.events import ContinuityDisposition, EventContinuityTracker

pytestmark = pytest.mark.stress


def bar(sequence: int, timestamp: datetime) -> MarketEvent:
    return MarketEvent(
        version=LifecycleVersion.V1,
        event_time=timestamp,
        receipt_time=timestamp,
        kind=MarketEventKind.BAR,
        completion=EventCompletion.COMPLETE,
        source="stress",
        asset=f"ASSET-{sequence % 32}",
        payload=BarPayload(99.0, 101.0, 98.0, 100.0, 1_000.0),
        provider_sequence=sequence,
    )


def test_continuity_state_and_rss_stay_bounded_under_sustained_events() -> None:
    process = psutil.Process()
    tracker = EventContinuityTracker()
    started = datetime.now(UTC)
    gc.collect()
    baseline_rss = process.memory_info().rss

    for sequence in range(100_000):
        event = bar(sequence, started + timedelta(microseconds=sequence))
        assert tracker.validate(event) is ContinuityDisposition.ACCEPT

    snapshot = tracker.snapshot()
    gc.collect()
    rss_growth = process.memory_info().rss - baseline_rss

    assert snapshot["tracked_streams"] == 32
    assert snapshot["accepted_count"] == 100_000
    assert snapshot["duplicate_count"] == 0
    assert snapshot["violation_count"] == 0
    assert rss_growth < 20 * 1024 * 1024
