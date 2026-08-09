"""Unit tests for OKXFundingFeed."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ml4t.specs import (
    BarPayload,
    EventCompletion,
    FundingPayload,
    LifecycleVersion,
    MarketEvent,
    MarketEventKind,
)

from ml4t.live.feeds.okx_feed import OKXFundingFeed


def okx_bar(timestamp: datetime) -> MarketEvent:
    return MarketEvent(
        version=LifecycleVersion.V1,
        event_time=timestamp,
        receipt_time=timestamp,
        kind=MarketEventKind.BAR,
        completion=EventCompletion.COMPLETE,
        source="okx",
        asset="BTC-USDT-SWAP",
        payload=BarPayload(44_000, 44_500, 43_900, 44_200, 150),
        provider_sequence=int(timestamp.timestamp() * 1_000),
    )


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

    async def test_initial_event_identity_sets_empty(self):
        """Test that event identity sets start empty."""
        feed = OKXFundingFeed(
            symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"],
        )

        assert feed._emitted_bars == set()
        assert feed._emitted_funding == set()

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
                [str(ts_ms), "44000", "44500", "43900", "44200", "150", "0", "0", "1"],
            ],
        }

        mock_client = MockHttpxAsyncClient([MockHttpxResponse(response_data)])
        feed._client = mock_client

        result = await feed._fetch_latest_ohlcv("BTC-USDT-SWAP")

        assert result is not None
        assert result.event_time == datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        assert result.kind is MarketEventKind.BAR
        assert result.completion is EventCompletion.COMPLETE
        assert result.payload == BarPayload(44000.0, 44500.0, 43900.0, 44200.0, 150.0)
        assert result.provider_sequence == ts_ms

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
        assert result.payload == BarPayload(44000.0, 44500.0, 43900.0, 44200.0, 150.0)
        assert result.completion is EventCompletion.EVOLVING

    async def test_fetch_ohlcv_rejects_malformed_or_nonpositive_payload(self):
        feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP"])
        response_data = {
            "code": "0",
            "data": [["1704110400000", "0", "44500", "43900", "44200", "150"]],
        }
        feed._client = MockHttpxAsyncClient([MockHttpxResponse(response_data)])

        assert await feed._fetch_latest_ohlcv("BTC-USDT-SWAP") is None
        assert feed.stats["rejected_count"] == 1


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
        assert result.kind is MarketEventKind.FUNDING
        assert result.payload == FundingPayload(0.0001)
        assert result.metadata["next_funding_rate"] == 0.00015
        assert (
            result.metadata["next_funding_time"]
            == datetime(2024, 1, 1, 16, 0, 0, tzinfo=UTC).isoformat()
        )
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
        assert result.payload == FundingPayload(0.0002)
        assert result.metadata["next_funding_rate"] is None
        assert result.metadata["next_funding_time"] is None

    async def test_scheduled_funding_time_is_identity_not_future_event_time(self):
        feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP"])
        scheduled = datetime.now(UTC) + timedelta(hours=4)
        scheduled_ms = int(scheduled.timestamp() * 1_000)
        feed._client = MockHttpxAsyncClient(
            [
                MockHttpxResponse(
                    {
                        "code": "0",
                        "data": [
                            {
                                "fundingRate": "0.0001",
                                "fundingTime": str(scheduled_ms),
                            }
                        ],
                    }
                )
            ]
        )

        event = await feed._fetch_funding_rate("BTC-USDT-SWAP")

        assert event is not None
        assert event.event_time == event.receipt_time
        assert event.event_time < scheduled
        assert event.provider_sequence == scheduled_ms
        assert (
            event.metadata["funding_time"]
            == datetime.fromtimestamp(scheduled_ms / 1_000, tz=UTC).isoformat()
        )

    async def test_missing_funding_rate_is_rejected(self):
        feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP"])
        feed._client = MockHttpxAsyncClient(
            [MockHttpxResponse({"code": "0", "data": [{"fundingTime": "1704110400000"}]})]
        )

        assert await feed._fetch_funding_rate("BTC-USDT-SWAP") is None
        assert feed.stats["rejected_count"] == 1

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
                [str(ts_ms), "44000", "44500", "43900", "44200", "150", "0", "0", "1"],
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
        assert item.event_time == datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        assert item.kind is MarketEventKind.BAR
        assert item.payload == BarPayload(44000.0, 44500.0, 43900.0, 44200.0, 150.0)
        funding = await asyncio.wait_for(feed._queue.get(), timeout=1.0)
        assert funding is not None
        assert funding.kind is MarketEventKind.FUNDING
        assert funding.payload == FundingPayload(0.0001)
        assert feed._bar_count == 1

    async def test_duplicate_bar_filtering(self):
        """Test that duplicate bars are filtered by timestamp."""
        feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP"])

        ts_ms = int(datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC).timestamp() * 1000)
        ohlcv_response = {
            "code": "0",
            "data": [
                [str(ts_ms), "44000", "44500", "43900", "44200", "150", "0", "0", "1"],
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

        events = [await asyncio.wait_for(feed._queue.get(), timeout=1.0) for _ in range(2)]
        assert [event.kind for event in events if event is not None] == [
            MarketEventKind.BAR,
            MarketEventKind.FUNDING,
        ]
        assert feed._queue.empty()

    async def test_identical_evolving_bar_is_deduplicated_but_revision_emits(self):
        feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP"])
        timestamp = datetime(2024, 1, 1, 12, tzinfo=UTC)

        def evolving(close: float) -> MarketEvent:
            return MarketEvent(
                version=LifecycleVersion.V1,
                event_time=timestamp,
                receipt_time=datetime.now(UTC),
                kind=MarketEventKind.BAR,
                completion=EventCompletion.EVOLVING,
                source="okx",
                asset="BTC-USDT-SWAP",
                payload=BarPayload(44_000, 44_500, 43_900, close, 150),
                provider_sequence=int(timestamp.timestamp() * 1_000),
            )

        with (
            patch.object(
                feed,
                "_fetch_latest_ohlcv",
                AsyncMock(side_effect=[evolving(44_200), evolving(44_200), evolving(44_300)]),
            ),
            patch.object(feed, "_fetch_funding_rate", AsyncMock(return_value=None)),
        ):
            await feed._fetch_and_emit()
            await feed._fetch_and_emit()
            await feed._fetch_and_emit()

        assert feed.stats["bar_count"] == 2
        events = [feed._queue.get_nowait(), feed._queue.get_nowait()]
        assert [event.payload.close for event in events if event is not None] == [44_200, 44_300]

    async def test_funding_emits_when_candle_endpoint_has_no_data(self):
        feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP"])
        feed._client = MockHttpxAsyncClient(
            [
                MockHttpxResponse({"code": "0", "data": []}),
                MockHttpxResponse({"code": "0", "data": [{"fundingRate": "0.0001"}]}),
            ]
        )

        await feed._fetch_and_emit()

        event = feed._queue.get_nowait()
        assert event is not None
        assert event.kind is MarketEventKind.FUNDING

    async def test_multiple_symbols(self):
        """Test fetching data for multiple symbols."""
        feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP"])

        ts_ms = int(datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC).timestamp() * 1000)
        btc_ohlcv = {
            "code": "0",
            "data": [[str(ts_ms), "44000", "44500", "43900", "44200", "150", "0", "0", "1"]],
        }
        btc_funding = {"code": "0", "data": [{"fundingRate": "0.0001"}]}
        eth_ohlcv = {
            "code": "0",
            "data": [[str(ts_ms), "2200", "2250", "2180", "2220", "1000", "0", "0", "1"]],
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

        events = [await asyncio.wait_for(feed._queue.get(), timeout=1.0) for _ in range(4)]
        bars = [
            event for event in events if event is not None and event.kind is MarketEventKind.BAR
        ]
        assert {event.asset for event in bars} == {"BTC-USDT-SWAP", "ETH-USDT-SWAP"}


@pytest.mark.asyncio
class TestOKXFundingFeedAsyncIteration:
    """Test async iteration over the feed."""

    async def test_async_iteration(self):
        """Test basic async iteration."""
        feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP"])

        # Pre-populate queue
        ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        event = okx_bar(ts)
        feed._queue.put_nowait(event)
        feed._queue.put_nowait(None)  # Sentinel

        items = []
        async for item in feed:
            items.append(item)

        assert len(items) == 1
        assert items == [event]

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
            assert feed._running is False
            with pytest.raises(RuntimeError, match="polling failed"):
                await feed.__anext__()


@pytest.mark.asyncio
class TestOKXFundingFeedMinuteBars:
    """Tests specific to 1-minute candle support."""

    async def test_fetch_ohlcv_minute_bars_use_okx_1m_param(self):
        feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP"], timeframe="1m")

        ts_ms = int(datetime(2024, 1, 1, 12, 34, 0, tzinfo=UTC).timestamp() * 1000)
        response_data = {
            "code": "0",
            "data": [
                [str(ts_ms + 60000), "45000", "45100", "44900", "45050", "10", "0", "0", "0"],
                [str(ts_ms), "44900", "45000", "44850", "44950", "20", "0", "0", "1"],
            ],
        }

        class CapturingClient(MockHttpxAsyncClient):
            def __init__(self):
                super().__init__([MockHttpxResponse(response_data)])
                self.last_params = None

            async def _get(self, url: str, params: dict | None = None):
                self.last_params = params
                return await super()._get(url, params=params)

        mock_client = CapturingClient()
        feed._client = mock_client

        result = await feed._fetch_latest_ohlcv("BTC-USDT-SWAP")

        assert mock_client.last_params == {
            "instId": "BTC-USDT-SWAP",
            "bar": "1m",
            "limit": "2",
        }
        assert result is not None
        assert result.event_time == datetime(2024, 1, 1, 12, 34, 0, tzinfo=UTC)
        assert result.event_time.second == 0
        assert result.event_time.microsecond == 0
        assert result.payload == BarPayload(44900.0, 45000.0, 44850.0, 44950.0, 20.0)

    async def test_fetch_and_emit_minute_bar_keeps_funding_context(self):
        feed = OKXFundingFeed(symbols=["BTC-USDT-SWAP"], timeframe="1m")

        ts_ms = int(datetime(2024, 1, 1, 12, 35, 0, tzinfo=UTC).timestamp() * 1000)
        ohlcv_response = {
            "code": "0",
            "data": [
                [str(ts_ms + 60000), "45000", "45100", "44900", "45050", "10", "0", "0", "0"],
                [str(ts_ms), "44900", "45000", "44850", "44950", "20", "0", "0", "1"],
            ],
        }
        funding_response = {
            "code": "0",
            "data": [
                {
                    "fundingRate": "0.0001",
                    "nextFundingRate": "0.0002",
                    "nextFundingTime": str(ts_ms + 8 * 60 * 60 * 1000),
                }
            ],
        }

        mock_client = MockHttpxAsyncClient(
            [MockHttpxResponse(ohlcv_response), MockHttpxResponse(funding_response)]
        )
        feed._client = mock_client

        await feed._fetch_and_emit()
        bar_event = await asyncio.wait_for(feed._queue.get(), timeout=1.0)
        funding_event = await asyncio.wait_for(feed._queue.get(), timeout=1.0)

        assert bar_event is not None
        assert bar_event.event_time == datetime(2024, 1, 1, 12, 35, 0, tzinfo=UTC)
        assert bar_event.payload == BarPayload(44900.0, 45000.0, 44850.0, 44950.0, 20.0)
        assert funding_event is not None
        assert funding_event.payload == FundingPayload(0.0001)
        assert funding_event.metadata["next_funding_rate"] == 0.0002
