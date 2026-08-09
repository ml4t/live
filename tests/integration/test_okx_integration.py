"""Integration tests for the public OKX funding feed."""

from datetime import UTC

import httpx
import pytest

from ml4t.live.feeds.okx_feed import OKXFundingFeed

pytestmark = [pytest.mark.integration, pytest.mark.external]


@pytest.mark.asyncio
async def test_okx_public_minute_bar_and_funding_context():
    feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP"], timeframe="1m", poll_interval_seconds=5.0)
    feed._client = httpx.AsyncClient(timeout=30.0)

    try:
        ohlcv = await feed._fetch_latest_ohlcv("BTC-USDT-SWAP")
        funding = await feed._fetch_funding_rate("BTC-USDT-SWAP")
    finally:
        await feed.close()

    assert ohlcv is not None
    assert funding is not None

    timestamp, bar = ohlcv
    assert timestamp.tzinfo == UTC
    assert timestamp.second == 0
    assert timestamp.microsecond == 0
    assert bar["volume"] >= 0.0
    assert isinstance(funding["funding_rate"], float)
    if funding["next_funding_time"] is not None:
        assert funding["next_funding_time"].tzinfo == UTC
