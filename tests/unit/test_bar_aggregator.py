"""Causal contract tests for BarAggregator."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from ml4t.specs import (
    BarPayload,
    EventCompletion,
    GapEvidence,
    LifecycleVersion,
    MarketEvent,
    MarketEventKind,
    TradePayload,
)

from ml4t.live.feeds.aggregator import BarAggregator, BarBuffer
from ml4t.live.feeds.events import FeedContractError


class MockDataFeed:
    """Finite typed or legacy source used to exercise real async iteration."""

    def __init__(self, data: list[Any]) -> None:
        self.data = data
        self._started = False
        self._stopped = False

    async def start(self) -> None:
        self._started = True
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    async def __aiter__(self) -> AsyncIterator[Any]:
        for item in self.data:
            if self._stopped:
                return
            yield item
            await asyncio.sleep(0)


def trade(asset: str, timestamp: datetime, price: float, size: float = 0.0) -> MarketEvent:
    """Create one independently validated provider trade fixture."""
    return MarketEvent(
        version=LifecycleVersion.V1,
        event_time=timestamp,
        receipt_time=timestamp,
        kind=MarketEventKind.TRADE,
        completion=EventCompletion.COMPLETE,
        source="fixture",
        asset=asset,
        payload=TradePayload(price=price, size=size),
        provider_sequence=int(timestamp.timestamp() * 1_000_000),
    )


def bar(asset: str, timestamp: datetime, **payload: float) -> MarketEvent:
    """Create one independently validated provider bar fixture."""
    return MarketEvent(
        version=LifecycleVersion.V1,
        event_time=timestamp,
        receipt_time=timestamp,
        kind=MarketEventKind.BAR,
        completion=EventCompletion.COMPLETE,
        source="fixture",
        asset=asset,
        payload=BarPayload(**payload),
        gap=GapEvidence(False, "fixture sequence unavailable"),
    )


async def collect(aggregator: BarAggregator) -> list[MarketEvent]:
    """Run the real aggregator to source exhaustion and retain all output."""
    await aggregator.start()
    return [event async for event in aggregator]


@pytest.mark.asyncio
class TestBarAggregator:
    async def test_initialization_and_configuration_validation(self) -> None:
        source = MockDataFeed([])
        aggregator = BarAggregator(source, bar_size_minutes=1)

        assert aggregator.bar_size == timedelta(minutes=1)
        assert aggregator.flush_timeout == 2.0
        assert aggregator._buffers == {}
        assert aggregator._current_bar_start == {}
        assert not aggregator._running
        with pytest.raises(ValueError, match="positive"):
            BarAggregator(source, bar_size_minutes=0)
        with pytest.raises(ValueError, match="non-negative"):
            BarAggregator(source, flush_timeout_seconds=-1)

    async def test_truncates_to_configured_boundary(self) -> None:
        aggregator = BarAggregator(MockDataFeed([]), bar_size_minutes=5)
        timestamp = datetime(2024, 1, 1, 10, 37, 42, 123456, tzinfo=UTC)

        assert aggregator._truncate_to_bar(timestamp) == datetime(2024, 1, 1, 10, 35, tzinfo=UTC)

    async def test_boundary_and_shutdown_emit_exact_completed_bars(self) -> None:
        start = datetime(2024, 1, 1, 10, tzinfo=UTC)
        events = await collect(
            BarAggregator(
                MockDataFeed(
                    [
                        trade("AAPL", start, 150.0, 100),
                        trade("AAPL", start + timedelta(seconds=30), 151.0, 50),
                        trade("AAPL", start + timedelta(minutes=1), 152.0, 75),
                    ]
                )
            )
        )

        assert [event.event_time for event in events] == [
            start,
            start + timedelta(minutes=1),
        ]
        assert all(event.kind is MarketEventKind.BAR for event in events)
        assert all(event.completion is EventCompletion.COMPLETE for event in events)
        assert events[0].payload == BarPayload(150.0, 151.0, 150.0, 151.0, 150.0)
        assert events[0].metadata["bar_end"] == (start + timedelta(minutes=1)).isoformat()
        assert events[1].payload == BarPayload(152.0, 152.0, 152.0, 152.0, 75.0)

    async def test_bar_input_preserves_range_and_resets_between_intervals(self) -> None:
        start = datetime(2024, 1, 1, 10, tzinfo=UTC)
        events = await collect(
            BarAggregator(
                MockDataFeed(
                    [
                        bar(
                            "AAPL",
                            start,
                            open=149.0,
                            high=151.0,
                            low=148.0,
                            close=150.0,
                            volume=1_000.0,
                        ),
                        trade("AAPL", start + timedelta(minutes=1), 145.0, 75),
                    ]
                )
            )
        )

        assert events[0].payload == BarPayload(149.0, 151.0, 148.0, 150.0, 1_000.0)
        assert events[1].payload == BarPayload(145.0, 145.0, 145.0, 145.0, 75.0)

    async def test_sparse_assets_have_independent_boundaries(self) -> None:
        start = datetime(2024, 1, 1, 10, tzinfo=UTC)
        events = await collect(
            BarAggregator(
                MockDataFeed(
                    [
                        trade("AAPL", start, 150.0, 100),
                        trade("GOOGL", start + timedelta(seconds=30), 2_800.0, 50),
                        trade("AAPL", start + timedelta(minutes=1), 151.0, 75),
                        trade("GOOGL", start + timedelta(minutes=2), 2_805.0, 20),
                    ]
                )
            )
        )

        identities = [(event.asset, event.event_time) for event in events]
        assert identities == [
            ("AAPL", start),
            ("GOOGL", start),
            ("AAPL", start + timedelta(minutes=1)),
            ("GOOGL", start + timedelta(minutes=2)),
        ]

    async def test_assets_filter_excludes_unselected_source_events(self) -> None:
        start = datetime(2024, 1, 1, 10, tzinfo=UTC)
        events = await collect(
            BarAggregator(
                MockDataFeed([trade("AAPL", start, 150.0), trade("GOOGL", start, 2_800.0)]),
                assets=["AAPL"],
            )
        )

        assert [event.asset for event in events] == ["AAPL"]

    async def test_late_input_cannot_reopen_completed_bar(self) -> None:
        start = datetime(2024, 1, 1, 10, tzinfo=UTC)
        events = await collect(
            BarAggregator(
                MockDataFeed(
                    [
                        trade("AAPL", start, 150.0),
                        trade("AAPL", start + timedelta(minutes=1), 151.0),
                        trade("AAPL", start + timedelta(seconds=30), 999.0),
                    ]
                )
            )
        )

        assert [event.event_time for event in events] == [start, start + timedelta(minutes=1)]
        assert events[0].payload.close == 150.0
        assert events[1].payload.close == 151.0

    async def test_current_interval_shutdown_flush_is_evolving(self) -> None:
        now = datetime.now(UTC)
        start = now.replace(second=0, microsecond=0)
        event = MarketEvent(
            version=LifecycleVersion.V1,
            event_time=now,
            receipt_time=now,
            kind=MarketEventKind.TRADE,
            completion=EventCompletion.COMPLETE,
            source="fixture",
            asset="AAPL",
            payload=TradePayload(price=150.0, size=10.0),
            provider_sequence=1,
        )

        events = await collect(BarAggregator(MockDataFeed([event])))

        assert len(events) == 1
        assert events[0].event_time == start
        assert events[0].completion is EventCompletion.EVOLVING

    async def test_flush_checker_completes_elapsed_interval(self) -> None:
        aggregator = BarAggregator(MockDataFeed([]), flush_timeout_seconds=0.0)
        start = datetime(2024, 1, 1, 10, tzinfo=UTC)
        aggregator._running = True
        aggregator._current_bar_start["AAPL"] = start
        aggregator._buffers["AAPL"] = BarBuffer()
        aggregator._buffers["AAPL"].update(150.0, 100)

        task = asyncio.create_task(aggregator._flush_checker())
        try:
            event = await asyncio.wait_for(aggregator._queue.get(), timeout=1.5)
        finally:
            aggregator._running = False
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert isinstance(event, MarketEvent)
        assert event.event_time == start
        assert event.completion is EventCompletion.COMPLETE
        assert event.payload.close == 150.0

    async def test_invalid_legacy_payload_failure_reaches_consumer(self) -> None:
        start = datetime(2024, 1, 1, 10, tzinfo=UTC)
        aggregator = BarAggregator(MockDataFeed([(start, {"AAPL": {"price": float("nan")}}, {})]))

        await aggregator.start()
        with pytest.raises(ValueError, match="finite"):
            _ = [event async for event in aggregator]

    async def test_naive_legacy_timestamp_failure_reaches_consumer(self) -> None:
        start = datetime(2024, 1, 1, 10)
        aggregator = BarAggregator(MockDataFeed([(start, {"AAPL": {"price": 150.0}}, {})]))

        await aggregator.start()
        with pytest.raises(FeedContractError, match="timezone-aware UTC"):
            _ = [event async for event in aggregator]

    async def test_empty_feed_stops_without_events(self) -> None:
        aggregator = BarAggregator(MockDataFeed([]))

        assert await collect(aggregator) == []
