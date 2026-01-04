"""Unit tests for DataBentoFeed."""

import asyncio
from datetime import datetime
from unittest.mock import patch

import pytest

from ml4t.live.feeds.databento_feed import DataBentoFeed


class MockRecord:
    """Mock DataBento record."""

    def __init__(self, record_type='ohlcv', symbol='SPY', timestamp_ns=None):
        self.symbol = symbol
        self.ts_event = timestamp_ns or int(datetime.now().timestamp() * 1e9)

        if record_type == 'ohlcv':
            self.open = int(450.0 * 1e9)
            self.high = int(451.0 * 1e9)
            self.low = int(449.0 * 1e9)
            self.close = int(450.5 * 1e9)
            self.volume = 10000
        elif record_type == 'trade':
            self.price = int(450.0 * 1e9)
            self.size = 100
        elif record_type == 'mbp':
            self.bid_px_00 = int(449.99 * 1e9)
            self.ask_px_00 = int(450.01 * 1e9)
            self.bid_sz_00 = 200
            self.ask_sz_00 = 150


class MockDBNStore:
    """Mock DataBento DBNStore."""

    def __init__(self, records):
        self.records = records
        self._index = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self._index >= len(self.records):
            raise StopIteration
        record = self.records[self._index]
        self._index += 1
        return record


class MockLiveClient:
    """Mock DataBento Live client."""

    def __init__(self, records):
        self.records = records
        self._index = 0

    def subscribe(self, dataset, schema, symbols):
        self.dataset = dataset
        self.schema = schema
        self.symbols = symbols

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self.records):
            raise StopAsyncIteration
        record = self.records[self._index]
        self._index += 1
        await asyncio.sleep(0.01)  # Simulate delay
        return record


def requires_databento_available():
    """Decorator to skip tests when databento is not available."""
    try:
        import databento  # noqa: F401

        return pytest.mark.asyncio
    except ImportError:
        return pytest.mark.skip(reason="databento not installed")


@pytest.mark.asyncio
class TestDataBentoFeed:
    """Test suite for DataBentoFeed.

    Note: These tests use mocks and don't require actual databento package.
    """

    async def test_initialization_mock(self):
        """Test DataBentoFeed initialization with mock."""
        with patch('ml4t.live.feeds.databento_feed.DATABENTO_AVAILABLE', True):
            mock_store = MockDBNStore([])
            feed = DataBentoFeed(
                client=mock_store,
                symbols=['SPY'],
                mode='historical',
                replay_speed=1.0,
            )

            assert feed.client == mock_store
            assert feed.symbols == ['SPY']
            assert feed.mode == 'historical'
            assert feed.replay_speed == 1.0
            assert not feed._running

    async def test_start_historical_mock(self):
        """Test starting historical feed with mock."""
        with patch('ml4t.live.feeds.databento_feed.DATABENTO_AVAILABLE', True):
            mock_records = [MockRecord('ohlcv', 'SPY')]
            mock_store = MockDBNStore(mock_records)
            feed = DataBentoFeed(mock_store, symbols=['SPY'], mode='historical')

            await feed.start()

            assert feed._running
            assert feed._replay_task is not None

            feed.stop()

    async def test_start_live_mock(self):
        """Test starting live feed with mock."""
        with patch('ml4t.live.feeds.databento_feed.DATABENTO_AVAILABLE', True):
            mock_records = [MockRecord('ohlcv', 'SPY')]
            mock_client = MockLiveClient(mock_records)
            feed = DataBentoFeed(mock_client, symbols=['SPY'], mode='live')

            await feed.start()

            assert feed._running
            assert feed._replay_task is not None

            feed.stop()

    async def test_stop_mock(self):
        """Test stopping feed with mock."""
        with patch('ml4t.live.feeds.databento_feed.DATABENTO_AVAILABLE', True):
            mock_store = MockDBNStore([])
            feed = DataBentoFeed(mock_store, symbols=['SPY'], mode='historical')

            await feed.start()
            feed.stop()

            assert not feed._running
            # Check queue got None sentinel
            sentinel = await asyncio.wait_for(feed._queue.get(), timeout=1.0)
            assert sentinel is None

    async def test_convert_ohlcv_record_mock(self):
        """Test converting OHLCV record with mock."""
        with patch('ml4t.live.feeds.databento_feed.DATABENTO_AVAILABLE', True):
            mock_store = MockDBNStore([])
            feed = DataBentoFeed(mock_store, symbols=['SPY'], mode='historical')

            record = MockRecord('ohlcv', 'SPY')
            timestamp, data, context = feed._convert_record(record)

            assert isinstance(timestamp, datetime)
            assert 'SPY' in data
            assert data['SPY']['open'] == 450.0
            assert data['SPY']['high'] == 451.0
            assert data['SPY']['low'] == 449.0
            assert data['SPY']['close'] == 450.5
            assert data['SPY']['volume'] == 10000

    async def test_convert_trade_record_mock(self):
        """Test converting Trade record with mock."""
        with patch('ml4t.live.feeds.databento_feed.DATABENTO_AVAILABLE', True):
            mock_store = MockDBNStore([])
            feed = DataBentoFeed(mock_store, symbols=['SPY'], mode='historical')

            record = MockRecord('trade', 'SPY')
            timestamp, data, context = feed._convert_record(record)

            assert isinstance(timestamp, datetime)
            assert 'SPY' in data
            assert data['SPY']['price'] == 450.0
            assert data['SPY']['size'] == 100

    async def test_convert_mbp_record_mock(self):
        """Test converting MBP (market by price) record with mock."""
        with patch('ml4t.live.feeds.databento_feed.DATABENTO_AVAILABLE', True):
            mock_store = MockDBNStore([])
            feed = DataBentoFeed(mock_store, symbols=['SPY'], mode='historical')

            record = MockRecord('mbp', 'SPY')
            timestamp, data, context = feed._convert_record(record)

            assert isinstance(timestamp, datetime)
            assert 'SPY' in data
            assert data['SPY']['price'] == 449.99  # Best bid
            assert data['SPY']['size'] == 200
            assert 'SPY' in context
            assert context['SPY']['bid'] == 449.99
            assert context['SPY']['ask'] == 450.01

    async def test_historical_replay_timing_mock(self):
        """Test historical replay respects timing with mock."""
        with patch('ml4t.live.feeds.databento_feed.DATABENTO_AVAILABLE', True):
            # Create records 1 second apart
            base_time = int(datetime(2024, 1, 1, 10, 0, 0).timestamp() * 1e9)
            records = [
                MockRecord('ohlcv', 'SPY', base_time),
                MockRecord('ohlcv', 'SPY', base_time + int(1e9)),  # +1 second
                MockRecord('ohlcv', 'SPY', base_time + int(2e9)),  # +2 seconds
            ]
            mock_store = MockDBNStore(records)
            feed = DataBentoFeed(mock_store, symbols=['SPY'], mode='historical', replay_speed=10.0)

            await feed.start()

            start = asyncio.get_event_loop().time()
            items = []

            async def collect():
                async for item in feed:
                    items.append(item)

            try:
                await asyncio.wait_for(collect(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

            elapsed = asyncio.get_event_loop().time() - start

            # 2 seconds of data at 10x speed = 0.2 seconds
            # Allow some tolerance
            assert len(items) == 3
            assert elapsed < 0.5  # Should be ~0.2s with 10x speed

            feed.stop()

    async def test_async_iteration_mock(self):
        """Test async iteration over feed with mock."""
        with patch('ml4t.live.feeds.databento_feed.DATABENTO_AVAILABLE', True):
            records = [
                MockRecord('ohlcv', 'SPY'),
                MockRecord('ohlcv', 'QQQ'),
            ]
            mock_store = MockDBNStore(records)
            feed = DataBentoFeed(mock_store, symbols=['SPY', 'QQQ'], mode='historical', replay_speed=0)

            await feed.start()

            items = []
            async for item in feed:
                items.append(item)

            assert len(items) == 2
            assert items[0][1]['SPY']['open'] == 450.0
            assert items[1][1]['QQQ']['open'] == 450.0

    async def test_stats_mock(self):
        """Test feed statistics with mock."""
        with patch('ml4t.live.feeds.databento_feed.DATABENTO_AVAILABLE', True):
            records = [MockRecord('ohlcv', 'SPY') for _ in range(5)]
            mock_store = MockDBNStore(records)
            feed = DataBentoFeed(mock_store, symbols=['SPY'], mode='historical', replay_speed=0)

            await feed.start()

            # Consume all records
            async for _ in feed:
                pass

            stats = feed.stats
            assert stats['mode'] == 'historical'
            assert stats['record_count'] == 5
            assert stats['symbols'] == ['SPY']
            assert stats['replay_speed'] == 0

    async def test_live_streaming_mock(self):
        """Test live streaming mode with mock."""
        with patch('ml4t.live.feeds.databento_feed.DATABENTO_AVAILABLE', True):
            records = [
                MockRecord('ohlcv', 'ES.FUT'),
                MockRecord('ohlcv', 'NQ.FUT'),
            ]
            mock_client = MockLiveClient(records)
            feed = DataBentoFeed(mock_client, symbols=['ES.FUT', 'NQ.FUT'], mode='live')

            await feed.start()

            items = []
            async for item in feed:
                items.append(item)

            assert len(items) == 2
            assert 'ES.FUT' in items[0][1]
            assert 'NQ.FUT' in items[1][1]

            feed.stop()

    async def test_missing_dependency(self):
        """Test error when databento not installed."""
        with patch('ml4t.live.feeds.databento_feed.DATABENTO_AVAILABLE', False):
            with pytest.raises(ImportError, match="databento package required"):
                DataBentoFeed(None, symbols=['SPY'], mode='historical')

    async def test_stop_during_iteration_mock(self):
        """Test that stop() terminates iteration cleanly with mock."""
        with patch('ml4t.live.feeds.databento_feed.DATABENTO_AVAILABLE', True):
            # Create many records
            records = [MockRecord('ohlcv', 'SPY') for _ in range(100)]
            mock_store = MockDBNStore(records)
            feed = DataBentoFeed(mock_store, symbols=['SPY'], mode='historical', replay_speed=0)

            await feed.start()

            count = 0
            async for item in feed:
                count += 1
                if count >= 3:
                    feed.stop()
                    break

            assert count >= 3
            assert not feed._running

    async def test_zero_replay_speed_mock(self):
        """Test replay_speed=0 disables sleep (fast replay) with mock."""
        with patch('ml4t.live.feeds.databento_feed.DATABENTO_AVAILABLE', True):
            base_time = int(datetime(2024, 1, 1, 10, 0, 0).timestamp() * 1e9)
            records = [
                MockRecord('ohlcv', 'SPY', base_time),
                MockRecord('ohlcv', 'SPY', base_time + int(10e9)),  # +10 seconds
            ]
            mock_store = MockDBNStore(records)
            feed = DataBentoFeed(mock_store, symbols=['SPY'], mode='historical', replay_speed=0)

            await feed.start()

            start = asyncio.get_event_loop().time()
            items = []

            async def collect():
                async for item in feed:
                    items.append(item)

            await asyncio.wait_for(collect(), timeout=1.0)

            elapsed = asyncio.get_event_loop().time() - start

            # Should be nearly instant with replay_speed=0
            assert len(items) == 2
            assert elapsed < 0.1
