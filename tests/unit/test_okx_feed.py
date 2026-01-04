"""Unit tests for OKXFundingFeed."""

import asyncio
from datetime import datetime, UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ml4t.live.feeds.okx_feed import OKXFundingFeed


class MockHttpxResponse:
    """Mock httpx response object."""

    def __init__(self, data: dict, status_code: int = 200):
        self._data = data
        self.status_code = status_code

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=MagicMock(),
                response=MagicMock(status_code=self.status_code),
            )


class MockHttpxAsyncClient:
    """Mock httpx.AsyncClient for testing."""

    def __init__(self, responses: list | None = None):
        self._responses = responses or []
        self._call_count = 0
        self._closed = False
        self.get = AsyncMock(side_effect=self._get)

    async def _get(self, url: str, params: dict | None = None):
        if self._responses:
            response = self._responses[self._call_count % len(self._responses)]
            self._call_count += 1
            return response
        return MockHttpxResponse({"code": "0", "data": []})

    async def aclose(self):
        self._closed = True


@pytest.mark.asyncio
class TestOKXFundingFeedInitialization:
    """Test OKXFundingFeed initialization."""

    async def test_default_initialization(self):
        """Test feed initialization with default parameters."""
        feed = OKXFundingFeed(
            symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
        )

        assert feed.symbols == ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
        assert feed.timeframe == "1H"
        assert feed.poll_interval == 60.0
        assert not feed._running
        assert feed._client is None
        assert feed._poll_task is None

    async def test_custom_initialization(self):
        """Test feed initialization with custom parameters."""
        feed = OKXFundingFeed(
            symbols=["SOL-USDT-SWAP"],
            timeframe="4H",
            poll_interval_seconds=120.0,
        )

        assert feed.symbols == ["SOL-USDT-SWAP"]
        assert feed.timeframe == "4H"
        assert feed.poll_interval == 120.0

    async def test_initial_timestamps_empty(self):
        """Test that last_timestamps initialized for each symbol."""
        feed = OKXFundingFeed(
            symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"],
        )

        assert len(feed._last_timestamps) == 3
        for symbol in feed.symbols:
            assert symbol in feed._last_timestamps
            assert feed._last_timestamps[symbol] is None

    async def test_initial_statistics(self):
        """Test initial statistics are zero."""
        feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP"])

        assert feed._bar_count == 0
        assert feed._funding_updates == 0


@pytest.mark.asyncio
class TestOKXFundingFeedLifecycle:
    """Test OKXFundingFeed start/stop/close lifecycle."""

    async def test_start_creates_client(self):
        """Test start() creates httpx client and poll task."""
        feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP"])

        with patch("ml4t.live.feeds.okx_feed.httpx.AsyncClient") as mock_client_class:
            mock_client = MockHttpxAsyncClient()
            mock_client_class.return_value = mock_client

            await feed.start()

            assert feed._running is True
            assert feed._client is not None
            assert feed._poll_task is not None
            mock_client_class.assert_called_once_with(timeout=30.0)

            # Clean up
            feed.stop()
            await asyncio.sleep(0.1)

    async def test_stop_cancels_task(self):
        """Test stop() cancels poll task and queues sentinel."""
        feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP"])

        with patch("ml4t.live.feeds.okx_feed.httpx.AsyncClient") as mock_client_class:
            mock_client = MockHttpxAsyncClient()
            mock_client_class.return_value = mock_client

            await feed.start()
            assert feed._running is True

            feed.stop()

            assert feed._running is False
            # Sentinel should be in queue
            item = await asyncio.wait_for(feed._queue.get(), timeout=1.0)
            assert item is None

    async def test_close_closes_client(self):
        """Test close() closes the httpx client."""
        feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP"])

        with patch("ml4t.live.feeds.okx_feed.httpx.AsyncClient") as mock_client_class:
            mock_client = MockHttpxAsyncClient()
            mock_client_class.return_value = mock_client

            await feed.start()
            feed.stop()
            await feed.close()

            assert mock_client._closed is True

    async def test_close_without_client(self):
        """Test close() when client was never created."""
        feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP"])
        # Should not raise
        await feed.close()


@pytest.mark.asyncio
class TestOKXFundingFeedFetchOHLCV:
    """Test _fetch_latest_ohlcv method."""

    async def test_fetch_ohlcv_success(self):
        """Test successful OHLCV fetch and parsing."""
        feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP"])

        # OKX returns timestamp in milliseconds
        ts_ms = int(datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC).timestamp() * 1000)
        response_data = {
            "code": "0",
            "data": [
                # Current (incomplete) bar
                [str(ts_ms + 3600000), "45000", "45500", "44900", "45200", "100", "0", "0", "0"],
                # Complete bar (use this one)
                [str(ts_ms), "44000", "44500", "43900", "44200", "150", "0", "0", "0"],
            ],
        }

        mock_client = MockHttpxAsyncClient([MockHttpxResponse(response_data)])
        feed._client = mock_client

        result = await feed._fetch_latest_ohlcv("BTC-USDT-SWAP")

        assert result is not None
        timestamp, bar_data = result
        assert timestamp == datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        assert bar_data["open"] == 44000.0
        assert bar_data["high"] == 44500.0
        assert bar_data["low"] == 43900.0
        assert bar_data["close"] == 44200.0
        assert bar_data["volume"] == 150.0

    async def test_fetch_ohlcv_api_error(self):
        """Test OHLCV fetch with API error response."""
        feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP"])

        response_data = {
            "code": "50001",
            "msg": "System error",
            "data": [],
        }

        mock_client = MockHttpxAsyncClient([MockHttpxResponse(response_data)])
        feed._client = mock_client

        result = await feed._fetch_latest_ohlcv("BTC-USDT-SWAP")
        assert result is None

    async def test_fetch_ohlcv_empty_data(self):
        """Test OHLCV fetch with empty data."""
        feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP"])

        response_data = {"code": "0", "data": []}

        mock_client = MockHttpxAsyncClient([MockHttpxResponse(response_data)])
        feed._client = mock_client

        result = await feed._fetch_latest_ohlcv("BTC-USDT-SWAP")
        assert result is None

    async def test_fetch_ohlcv_no_client(self):
        """Test OHLCV fetch when client is None."""
        feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP"])
        feed._client = None

        result = await feed._fetch_latest_ohlcv("BTC-USDT-SWAP")
        assert result is None

    async def test_fetch_ohlcv_network_error(self):
        """Test OHLCV fetch with network error."""
        feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP"])

        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=Exception("Network error"))
        feed._client = mock_client

        result = await feed._fetch_latest_ohlcv("BTC-USDT-SWAP")
        assert result is None

    async def test_fetch_ohlcv_single_candle(self):
        """Test OHLCV fetch when only one candle returned."""
        feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP"])

        ts_ms = int(datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC).timestamp() * 1000)
        response_data = {
            "code": "0",
            "data": [
                [str(ts_ms), "44000", "44500", "43900", "44200", "150", "0", "0", "0"],
            ],
        }

        mock_client = MockHttpxAsyncClient([MockHttpxResponse(response_data)])
        feed._client = mock_client

        result = await feed._fetch_latest_ohlcv("BTC-USDT-SWAP")

        assert result is not None
        timestamp, bar_data = result
        assert bar_data["close"] == 44200.0


@pytest.mark.asyncio
class TestOKXFundingFeedFetchFundingRate:
    """Test _fetch_funding_rate method."""

    async def test_fetch_funding_rate_success(self):
        """Test successful funding rate fetch."""
        feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP"])

        next_funding_ts = int(datetime(2024, 1, 1, 16, 0, 0, tzinfo=UTC).timestamp() * 1000)
        response_data = {
            "code": "0",
            "data": [
                {
                    "fundingRate": "0.0001",
                    "nextFundingRate": "0.00015",
                    "nextFundingTime": str(next_funding_ts),
                }
            ],
        }

        mock_client = MockHttpxAsyncClient([MockHttpxResponse(response_data)])
        feed._client = mock_client

        result = await feed._fetch_funding_rate("BTC-USDT-SWAP")

        assert result is not None
        assert result["funding_rate"] == 0.0001
        assert result["next_funding_rate"] == 0.00015
        assert result["next_funding_time"] == datetime(2024, 1, 1, 16, 0, 0, tzinfo=UTC)
        assert feed._funding_updates == 1

    async def test_fetch_funding_rate_missing_next(self):
        """Test funding rate fetch with missing next funding data."""
        feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP"])

        response_data = {
            "code": "0",
            "data": [
                {
                    "fundingRate": "0.0002",
                }
            ],
        }

        mock_client = MockHttpxAsyncClient([MockHttpxResponse(response_data)])
        feed._client = mock_client

        result = await feed._fetch_funding_rate("BTC-USDT-SWAP")

        assert result is not None
        assert result["funding_rate"] == 0.0002
        assert result["next_funding_rate"] is None
        assert result["next_funding_time"] is None

    async def test_fetch_funding_rate_api_error(self):
        """Test funding rate fetch with API error."""
        feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP"])

        response_data = {"code": "50001", "msg": "Error", "data": []}

        mock_client = MockHttpxAsyncClient([MockHttpxResponse(response_data)])
        feed._client = mock_client

        result = await feed._fetch_funding_rate("BTC-USDT-SWAP")
        assert result is None

    async def test_fetch_funding_rate_no_client(self):
        """Test funding rate fetch when client is None."""
        feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP"])
        feed._client = None

        result = await feed._fetch_funding_rate("BTC-USDT-SWAP")
        assert result is None

    async def test_fetch_funding_rate_network_error(self):
        """Test funding rate fetch with network error."""
        feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP"])

        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=Exception("Connection failed"))
        feed._client = mock_client

        result = await feed._fetch_funding_rate("BTC-USDT-SWAP")
        assert result is None


@pytest.mark.asyncio
class TestOKXFundingFeedEmission:
    """Test _fetch_and_emit and data queuing."""

    async def test_fetch_and_emit_queues_data(self):
        """Test that _fetch_and_emit queues data correctly."""
        feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP"])

        ts_ms = int(datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC).timestamp() * 1000)
        ohlcv_response = {
            "code": "0",
            "data": [
                [str(ts_ms + 3600000), "45000", "45500", "44900", "45200", "100", "0", "0", "0"],
                [str(ts_ms), "44000", "44500", "43900", "44200", "150", "0", "0", "0"],
            ],
        }
        funding_response = {
            "code": "0",
            "data": [{"fundingRate": "0.0001"}],
        }

        mock_client = MockHttpxAsyncClient(
            [MockHttpxResponse(ohlcv_response), MockHttpxResponse(funding_response)]
        )
        feed._client = mock_client

        await feed._fetch_and_emit()

        # Check data was queued
        item = await asyncio.wait_for(feed._queue.get(), timeout=1.0)
        assert item is not None
        timestamp, data, context = item

        assert timestamp == datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        assert "BTC-USDT-SWAP" in data
        assert data["BTC-USDT-SWAP"]["close"] == 44200.0
        assert "BTC-USDT-SWAP" in context
        assert context["BTC-USDT-SWAP"]["funding_rate"] == 0.0001
        assert feed._bar_count == 1

    async def test_duplicate_bar_filtering(self):
        """Test that duplicate bars are filtered by timestamp."""
        feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP"])

        ts_ms = int(datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC).timestamp() * 1000)
        ohlcv_response = {
            "code": "0",
            "data": [
                [str(ts_ms), "44000", "44500", "43900", "44200", "150", "0", "0", "0"],
            ],
        }
        funding_response = {"code": "0", "data": [{"fundingRate": "0.0001"}]}

        mock_client = MockHttpxAsyncClient(
            [MockHttpxResponse(ohlcv_response), MockHttpxResponse(funding_response)]
        )
        feed._client = mock_client

        # First fetch
        await feed._fetch_and_emit()
        assert feed._bar_count == 1

        # Second fetch with same timestamp - should be filtered
        await feed._fetch_and_emit()
        assert feed._bar_count == 1  # Still 1, no duplicate

        # Queue should only have one item
        item = await asyncio.wait_for(feed._queue.get(), timeout=1.0)
        assert item is not None
        # Queue should be empty now
        assert feed._queue.empty()

    async def test_multiple_symbols(self):
        """Test fetching data for multiple symbols."""
        feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP"])

        ts_ms = int(datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC).timestamp() * 1000)
        btc_ohlcv = {
            "code": "0",
            "data": [[str(ts_ms), "44000", "44500", "43900", "44200", "150", "0", "0", "0"]],
        }
        btc_funding = {"code": "0", "data": [{"fundingRate": "0.0001"}]}
        eth_ohlcv = {
            "code": "0",
            "data": [[str(ts_ms), "2200", "2250", "2180", "2220", "1000", "0", "0", "0"]],
        }
        eth_funding = {"code": "0", "data": [{"fundingRate": "0.0002"}]}

        mock_client = MockHttpxAsyncClient(
            [
                MockHttpxResponse(btc_ohlcv),
                MockHttpxResponse(btc_funding),
                MockHttpxResponse(eth_ohlcv),
                MockHttpxResponse(eth_funding),
            ]
        )
        feed._client = mock_client

        await feed._fetch_and_emit()

        # Should have 2 items in queue
        assert feed._bar_count == 2

        item1 = await asyncio.wait_for(feed._queue.get(), timeout=1.0)
        item2 = await asyncio.wait_for(feed._queue.get(), timeout=1.0)

        # Check both symbols were emitted
        symbols_emitted = set()
        for item in [item1, item2]:
            _, data, _ = item
            symbols_emitted.update(data.keys())

        assert "BTC-USDT-SWAP" in symbols_emitted
        assert "ETH-USDT-SWAP" in symbols_emitted


@pytest.mark.asyncio
class TestOKXFundingFeedAsyncIteration:
    """Test async iteration over the feed."""

    async def test_async_iteration(self):
        """Test basic async iteration."""
        feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP"])

        # Pre-populate queue
        ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        data = {"BTC-USDT-SWAP": {"open": 44000, "close": 44200}}
        context = {"BTC-USDT-SWAP": {"funding_rate": 0.0001}}
        feed._queue.put_nowait((ts, data, context))
        feed._queue.put_nowait(None)  # Sentinel

        items = []
        async for item in feed:
            items.append(item)

        assert len(items) == 1
        timestamp, data, context = items[0]
        assert timestamp == ts
        assert data["BTC-USDT-SWAP"]["close"] == 44200

    async def test_stop_iteration_on_sentinel(self):
        """Test StopAsyncIteration when sentinel received."""
        feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP"])

        # Only put sentinel
        feed._queue.put_nowait(None)

        with pytest.raises(StopAsyncIteration):
            await feed.__anext__()

    async def test_aiter_returns_self(self):
        """Test __aiter__ returns self."""
        feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP"])
        assert feed.__aiter__() is feed


@pytest.mark.asyncio
class TestOKXFundingFeedStats:
    """Test statistics property."""

    async def test_stats_initial(self):
        """Test stats with initial values."""
        feed = OKXFundingFeed(
            symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
            timeframe="4H",
            poll_interval_seconds=120.0,
        )

        stats = feed.stats

        assert stats["running"] is False
        assert stats["symbols"] == ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
        assert stats["timeframe"] == "4H"
        assert stats["bar_count"] == 0
        assert stats["funding_updates"] == 0
        assert stats["poll_interval"] == 120.0

    async def test_stats_after_activity(self):
        """Test stats after some activity."""
        feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP"])

        # Simulate activity
        feed._running = True
        feed._bar_count = 10
        feed._funding_updates = 5

        stats = feed.stats

        assert stats["running"] is True
        assert stats["bar_count"] == 10
        assert stats["funding_updates"] == 5


@pytest.mark.asyncio
class TestOKXFundingFeedPollLoop:
    """Test the polling loop behavior."""

    async def test_poll_loop_cancellation(self):
        """Test poll loop handles cancellation gracefully."""
        feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP"], poll_interval_seconds=0.1)

        with patch("ml4t.live.feeds.okx_feed.httpx.AsyncClient") as mock_client_class:
            mock_client = MockHttpxAsyncClient([MockHttpxResponse({"code": "0", "data": []})])
            mock_client_class.return_value = mock_client

            await feed.start()
            await asyncio.sleep(0.05)  # Let poll start
            feed.stop()
            await asyncio.sleep(0.15)  # Let cancellation propagate

            assert feed._running is False

    async def test_poll_loop_error_exits(self):
        """Test poll loop exits on error (logs and stops)."""
        feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP"], poll_interval_seconds=0.05)

        call_count = 0

        async def mock_fetch_and_emit():
            nonlocal call_count
            call_count += 1
            raise Exception("Test error")

        with patch.object(feed, "_fetch_and_emit", mock_fetch_and_emit):
            feed._running = True
            # Run poll loop - should exit on error
            await feed._poll_loop()

            # Error causes loop to exit after first call
            assert call_count == 1
