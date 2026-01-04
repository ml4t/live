"""Unit tests for BarAggregator."""

import asyncio
from datetime import datetime, timedelta

import pytest
from ml4t.live.feeds.aggregator import BarAggregator


class MockDataFeed:
    """Mock data feed for testing."""

    def __init__(self, data_sequence: list[tuple[datetime, dict, dict]]):
        """Initialize with sequence of (timestamp, data, context) tuples."""
        self.data = data_sequence
        self.index = 0
        self._started = False
        self._stopped = False

    async def start(self):
        """Start the mock feed."""
        self._started = True

    def stop(self):
        """Stop the mock feed."""
        self._stopped = True

    async def __aiter__(self):
        """Async iteration over mock data."""
        while self.index < len(self.data) and not self._stopped:
            timestamp, data, context = self.data[self.index]
            self.index += 1
            yield timestamp, data, context
            await asyncio.sleep(0.01)  # Small delay to simulate real feed


@pytest.mark.asyncio
class TestBarAggregator:
    """Test suite for BarAggregator."""

    async def test_initialization(self):
        """Test BarAggregator initialization."""
        mock_feed = MockDataFeed([])
        aggregator = BarAggregator(mock_feed, bar_size_minutes=1)

        assert aggregator.bar_size == timedelta(minutes=1)
        assert aggregator.flush_timeout == 2.0
        assert aggregator._buffers == {}
        assert aggregator._current_bar_start is None
        assert not aggregator._running

    async def test_custom_bar_size(self):
        """Test BarAggregator with custom bar size."""
        mock_feed = MockDataFeed([])
        aggregator = BarAggregator(mock_feed, bar_size_minutes=5)

        assert aggregator.bar_size == timedelta(minutes=5)

    async def test_truncate_to_bar_1min(self):
        """Test _truncate_to_bar for 1-minute bars."""
        mock_feed = MockDataFeed([])
        aggregator = BarAggregator(mock_feed, bar_size_minutes=1)

        dt = datetime(2024, 1, 1, 10, 35, 42, 123456)
        truncated = aggregator._truncate_to_bar(dt)

        assert truncated == datetime(2024, 1, 1, 10, 35, 0, 0)

    async def test_truncate_to_bar_5min(self):
        """Test _truncate_to_bar for 5-minute bars."""
        mock_feed = MockDataFeed([])
        aggregator = BarAggregator(mock_feed, bar_size_minutes=5)

        # 10:37:42 should truncate to 10:35:00 (nearest 5-min boundary)
        dt = datetime(2024, 1, 1, 10, 37, 42)
        truncated = aggregator._truncate_to_bar(dt)
        assert truncated == datetime(2024, 1, 1, 10, 35, 0, 0)

        # 10:33:00 should truncate to 10:30:00
        dt = datetime(2024, 1, 1, 10, 33, 0)
        truncated = aggregator._truncate_to_bar(dt)
        assert truncated == datetime(2024, 1, 1, 10, 30, 0, 0)

    async def test_single_tick_single_asset(self):
        """Test aggregation of single tick for single asset."""
        base_time = datetime(2024, 1, 1, 10, 0, 0)
        mock_data = [
            (base_time, {'AAPL': {'price': 150.0, 'size': 100}}, {}),
        ]
        mock_feed = MockDataFeed(mock_data)
        aggregator = BarAggregator(mock_feed)

        await aggregator.start()
        bars = []

        # Collect bars (should be empty as no bar boundary crossed)
        try:
            async with asyncio.timeout(0.5):
                async for timestamp, data, context in aggregator:
                    bars.append((timestamp, data, context))
        except asyncio.TimeoutError:
            pass

        aggregator.stop()

        # No bars emitted yet (need next minute to emit)
        assert len(bars) == 0

    async def test_bar_boundary_emission(self):
        """Test bar emission when crossing minute boundary."""
        base_time = datetime(2024, 1, 1, 10, 0, 0)
        mock_data = [
            (base_time, {'AAPL': {'price': 150.0, 'size': 100}}, {}),
            (base_time + timedelta(seconds=30), {'AAPL': {'price': 151.0, 'size': 50}}, {}),
            (base_time + timedelta(minutes=1), {'AAPL': {'price': 152.0, 'size': 75}}, {}),
        ]
        mock_feed = MockDataFeed(mock_data)
        aggregator = BarAggregator(mock_feed)

        await aggregator.start()
        bars = []

        try:
            async with asyncio.timeout(1.0):
                async for timestamp, data, context in aggregator:
                    bars.append((timestamp, data, context))
        except asyncio.TimeoutError:
            pass

        aggregator.stop()

        # Should have one bar for 10:00
        assert len(bars) == 1
        timestamp, data, context = bars[0]
        assert timestamp == datetime(2024, 1, 1, 10, 0, 0)
        assert 'AAPL' in data
        assert data['AAPL']['open'] == 150.0
        assert data['AAPL']['high'] == 151.0
        assert data['AAPL']['low'] == 150.0
        assert data['AAPL']['close'] == 151.0
        assert data['AAPL']['volume'] == 150

    async def test_multiple_assets(self):
        """Test aggregation of multiple assets."""
        base_time = datetime(2024, 1, 1, 10, 0, 0)
        mock_data = [
            (base_time, {
                'AAPL': {'price': 150.0, 'size': 100},
                'GOOGL': {'price': 2800.0, 'size': 50}
            }, {}),
            (base_time + timedelta(minutes=1), {'AAPL': {'price': 151.0, 'size': 75}}, {}),
        ]
        mock_feed = MockDataFeed(mock_data)
        aggregator = BarAggregator(mock_feed)

        await aggregator.start()
        bars = []

        try:
            async with asyncio.timeout(1.0):
                async for timestamp, data, context in aggregator:
                    bars.append((timestamp, data, context))
        except asyncio.TimeoutError:
            pass

        aggregator.stop()

        assert len(bars) == 1
        timestamp, data, context = bars[0]
        assert 'AAPL' in data
        assert 'GOOGL' in data
        assert data['AAPL']['open'] == 150.0
        assert data['GOOGL']['open'] == 2800.0

    async def test_ohlcv_bar_input(self):
        """Test handling OHLCV bar input (not just ticks)."""
        base_time = datetime(2024, 1, 1, 10, 0, 0)
        mock_data = [
            (base_time, {
                'AAPL': {'open': 149.0, 'high': 151.0, 'low': 148.0, 'close': 150.0, 'volume': 1000}
            }, {}),
            (base_time + timedelta(minutes=1), {'AAPL': {'close': 152.0, 'volume': 500}}, {}),
        ]
        mock_feed = MockDataFeed(mock_data)
        aggregator = BarAggregator(mock_feed)

        await aggregator.start()
        bars = []

        try:
            async with asyncio.timeout(1.0):
                async for timestamp, data, context in aggregator:
                    bars.append((timestamp, data, context))
        except asyncio.TimeoutError:
            pass

        aggregator.stop()

        assert len(bars) == 1
        timestamp, data, context = bars[0]
        # Should aggregate based on 'close' field
        assert data['AAPL']['close'] == 150.0
        assert data['AAPL']['volume'] == 1000

    async def test_multiple_bars(self):
        """Test emission of multiple consecutive bars."""
        base_time = datetime(2024, 1, 1, 10, 0, 0)
        mock_data = [
            (base_time, {'AAPL': {'price': 150.0, 'size': 100}}, {}),
            (base_time + timedelta(minutes=1), {'AAPL': {'price': 151.0, 'size': 50}}, {}),
            (base_time + timedelta(minutes=2), {'AAPL': {'price': 152.0, 'size': 75}}, {}),
            (base_time + timedelta(minutes=3), {'AAPL': {'price': 153.0, 'size': 25}}, {}),
        ]
        mock_feed = MockDataFeed(mock_data)
        aggregator = BarAggregator(mock_feed)

        await aggregator.start()
        bars = []

        try:
            async with asyncio.timeout(1.0):
                async for timestamp, data, context in aggregator:
                    bars.append((timestamp, data, context))
        except asyncio.TimeoutError:
            pass

        aggregator.stop()

        # Should have 3 bars (10:00, 10:01, 10:02)
        assert len(bars) == 3
        assert bars[0][0] == datetime(2024, 1, 1, 10, 0, 0)
        assert bars[1][0] == datetime(2024, 1, 1, 10, 1, 0)
        assert bars[2][0] == datetime(2024, 1, 1, 10, 2, 0)

    async def test_buffer_reset_between_bars(self):
        """Test that buffers are reset between bars."""
        base_time = datetime(2024, 1, 1, 10, 0, 0)
        mock_data = [
            (base_time, {'AAPL': {'price': 150.0, 'size': 100}}, {}),
            (base_time + timedelta(seconds=30), {'AAPL': {'price': 155.0, 'size': 50}}, {}),
            (base_time + timedelta(minutes=1), {'AAPL': {'price': 145.0, 'size': 75}}, {}),
            (base_time + timedelta(minutes=2), {'AAPL': {'price': 148.0, 'size': 25}}, {}),
        ]
        mock_feed = MockDataFeed(mock_data)
        aggregator = BarAggregator(mock_feed)

        await aggregator.start()
        bars = []

        try:
            async with asyncio.timeout(1.0):
                async for timestamp, data, context in aggregator:
                    bars.append((timestamp, data, context))
        except asyncio.TimeoutError:
            pass

        aggregator.stop()

        assert len(bars) == 2

        # First bar (10:00): high should be 155, low 150
        assert bars[0][1]['AAPL']['high'] == 155.0
        assert bars[0][1]['AAPL']['low'] == 150.0

        # Second bar (10:01): should be independent, not carry over previous high
        assert bars[1][1]['AAPL']['open'] == 145.0
        assert bars[1][1]['AAPL']['high'] == 145.0  # Only one tick
        assert bars[1][1]['AAPL']['low'] == 145.0

    async def test_5minute_bars(self):
        """Test 5-minute bar aggregation."""
        base_time = datetime(2024, 1, 1, 10, 0, 0)
        mock_data = [
            (base_time, {'AAPL': {'price': 150.0, 'size': 100}}, {}),
            (base_time + timedelta(minutes=2), {'AAPL': {'price': 151.0, 'size': 50}}, {}),
            (base_time + timedelta(minutes=4), {'AAPL': {'price': 152.0, 'size': 75}}, {}),
            (base_time + timedelta(minutes=5), {'AAPL': {'price': 153.0, 'size': 25}}, {}),
        ]
        mock_feed = MockDataFeed(mock_data)
        aggregator = BarAggregator(mock_feed, bar_size_minutes=5)

        await aggregator.start()
        bars = []

        try:
            async with asyncio.timeout(1.0):
                async for timestamp, data, context in aggregator:
                    bars.append((timestamp, data, context))
        except asyncio.TimeoutError:
            pass

        aggregator.stop()

        # Should have 1 bar (10:00-10:05)
        assert len(bars) == 1
        timestamp, data, context = bars[0]
        assert timestamp == datetime(2024, 1, 1, 10, 0, 0)
        assert data['AAPL']['open'] == 150.0
        assert data['AAPL']['high'] == 152.0
        assert data['AAPL']['low'] == 150.0
        assert data['AAPL']['close'] == 152.0
        assert data['AAPL']['volume'] == 225

    async def test_stop_signal(self):
        """Test that stop() terminates iteration."""
        base_time = datetime(2024, 1, 1, 10, 0, 0)
        # Infinite data source
        mock_data = [(base_time + timedelta(seconds=i), {'AAPL': {'price': 150.0, 'size': 10}}, {})
                     for i in range(1000)]
        mock_feed = MockDataFeed(mock_data)
        aggregator = BarAggregator(mock_feed)

        await aggregator.start()
        bars = []

        # Collect a few bars then stop
        count = 0
        async for timestamp, data, context in aggregator:
            bars.append((timestamp, data, context))
            count += 1
            if count >= 2:
                aggregator.stop()
                break

        # Should have stopped cleanly
        assert len(bars) >= 2

    async def test_empty_feed(self):
        """Test handling of empty data feed."""
        mock_feed = MockDataFeed([])
        aggregator = BarAggregator(mock_feed)

        await aggregator.start()
        bars = []

        try:
            async with asyncio.timeout(0.5):
                async for timestamp, data, context in aggregator:
                    bars.append((timestamp, data, context))
        except asyncio.TimeoutError:
            pass

        aggregator.stop()

        assert len(bars) == 0

    async def test_asset_appears_midstream(self):
        """Test asset appearing mid-stream (not in first tick)."""
        base_time = datetime(2024, 1, 1, 10, 0, 0)
        mock_data = [
            (base_time, {'AAPL': {'price': 150.0, 'size': 100}}, {}),
            (base_time + timedelta(seconds=30), {
                'AAPL': {'price': 151.0, 'size': 50},
                'GOOGL': {'price': 2800.0, 'size': 25}  # New asset
            }, {}),
            (base_time + timedelta(minutes=1), {'AAPL': {'price': 152.0, 'size': 75}}, {}),
        ]
        mock_feed = MockDataFeed(mock_data)
        aggregator = BarAggregator(mock_feed)

        await aggregator.start()
        bars = []

        try:
            async with asyncio.timeout(1.0):
                async for timestamp, data, context in aggregator:
                    bars.append((timestamp, data, context))
        except asyncio.TimeoutError:
            pass

        aggregator.stop()

        assert len(bars) == 1
        timestamp, data, context = bars[0]
        assert 'AAPL' in data
        assert 'GOOGL' in data
        assert data['GOOGL']['open'] == 2800.0

    async def test_no_data_for_asset_in_bar(self):
        """Test that assets with no data in a bar are not included."""
        base_time = datetime(2024, 1, 1, 10, 0, 0)
        mock_data = [
            (base_time, {'AAPL': {'price': 150.0, 'size': 100}}, {}),
            (base_time + timedelta(minutes=1), {'GOOGL': {'price': 2800.0, 'size': 50}}, {}),
            (base_time + timedelta(minutes=2), {'AAPL': {'price': 151.0, 'size': 75}}, {}),
        ]
        mock_feed = MockDataFeed(mock_data)
        aggregator = BarAggregator(mock_feed)

        await aggregator.start()
        bars = []

        try:
            async with asyncio.timeout(1.0):
                async for timestamp, data, context in aggregator:
                    bars.append((timestamp, data, context))
        except asyncio.TimeoutError:
            pass

        aggregator.stop()

        # Should have 2 bars
        assert len(bars) == 2
        # First bar only has AAPL
        assert 'AAPL' in bars[0][1]
        assert 'GOOGL' not in bars[0][1]
        # Second bar only has GOOGL
        assert 'GOOGL' in bars[1][1]
        assert 'AAPL' not in bars[1][1]
