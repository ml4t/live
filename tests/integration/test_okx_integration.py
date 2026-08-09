"""Integration tests for the public OKX funding feed."""

import asyncio
from datetime import UTC

import pytest
from ml4t.specs import EventCompletion, MarketEventKind

from ml4t.live import OKXFundingFeed

pytestmark = [pytest.mark.integration, pytest.mark.external]


@pytest.mark.asyncio
async def test_okx_public_iterator_emits_validated_bar_and_funding_events() -> None:
    feed = OKXFundingFeed(
        symbols=["BTC-USDT-SWAP"],
        timeframe="1m",
        poll_interval_seconds=1,
    )
    events = []
    await feed.start()
    try:
        async with asyncio.timeout(60):
            while {event.kind for event in events} != {
                MarketEventKind.BAR,
                MarketEventKind.FUNDING,
            }:
                events.append(await anext(feed))
    finally:
        feed.stop()
        await feed.close()

    assert all(event.source == "okx" for event in events)
    assert all(event.asset == "BTC-USDT-SWAP" for event in events)
    assert all(event.event_time.utcoffset() == UTC.utcoffset(event.event_time) for event in events)
    assert all(
        event.receipt_time.utcoffset() == UTC.utcoffset(event.receipt_time) for event in events
    )
    assert all(event.provider_sequence is not None for event in events)
    funding = next(event for event in events if event.kind is MarketEventKind.FUNDING)
    assert funding.completion is EventCompletion.COMPLETE
