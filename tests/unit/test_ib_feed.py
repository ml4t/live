"""Unit tests for IBDataFeed."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from ib_async import Stock
from ml4t.specs import MarketEventKind, QuotePayload, TradePayload

from ml4t.live.feeds.ib_feed import IBDataFeed


class MockIB:
    """Mock IB instance for testing."""

    def __init__(self):
        self.connected = True
        self.qualified_contracts = {}
        self.tickers = {}
        self.pendingTickersEvent = MockEvent()

    def isConnected(self):
        return self.connected

    async def qualifyContractsAsync(self, contract):
        """Mock contract qualification."""
        if contract.symbol in self.qualified_contracts:
            return [self.qualified_contracts[contract.symbol]]
        return [contract]  # Default: qualify all

    def reqMktData(self, contract, *args, **kwargs):
        """Mock market data request."""
        ticker = MockTicker(contract)
        self.tickers[contract.symbol] = ticker
        return ticker

    def cancelMktData(self, contract):
        """Mock cancel market data."""
        if contract.symbol in self.tickers:
            del self.tickers[contract.symbol]


class MockEvent:
    """Mock event for ib_async."""

    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def __isub__(self, handler):
        if handler in self.handlers:
            self.handlers.remove(handler)
        return self

    def emit(self, *args, **kwargs):
        """Emit event to all handlers."""
        for handler in self.handlers:
            handler(*args, **kwargs)


class MockTicker:
    """Mock Ticker for testing."""

    def __init__(self, contract):
        self.contract = contract
        self.last = None
        self.lastSize = None
        self.bid = None
        self.ask = None
        self.bidSize = None
        self.askSize = None
        self.volume = None
        self.time = None


@pytest.mark.asyncio
class TestIBDataFeed:
    """Test suite for IBDataFeed."""

    async def test_initialization(self):
        """Test IBDataFeed initialization."""
        mock_ib = MockIB()
        feed = IBDataFeed(mock_ib, symbols=["SPY", "QQQ"])

        assert feed.ib == mock_ib
        assert feed.symbols == ["SPY", "QQQ"]
        assert feed.exchange == "SMART"
        assert feed.currency == "USD"
        assert feed.tick_throttle_ms == 100
        assert not feed._running
        assert feed._contracts == {}
        assert feed._tickers == {}

    async def test_custom_settings(self):
        """Test IBDataFeed with custom settings."""
        mock_ib = MockIB()
        feed = IBDataFeed(
            mock_ib,
            symbols=["SPY"],
            exchange="NYSE",
            currency="USD",
            tick_throttle_ms=500,
        )

        assert feed.exchange == "NYSE"
        assert feed.tick_throttle_ms == 500

    async def test_invalid_configuration_is_rejected(self):
        mock_ib = MockIB()
        with pytest.raises(ValueError, match="at least one"):
            IBDataFeed(mock_ib, symbols=[])
        with pytest.raises(ValueError, match="non-negative"):
            IBDataFeed(mock_ib, symbols=["SPY"], tick_throttle_ms=-1)

    async def test_start_success(self):
        """Test successful feed start."""
        mock_ib = MockIB()
        feed = IBDataFeed(mock_ib, symbols=["SPY", "QQQ"])

        await feed.start()

        assert feed._running
        assert len(feed._contracts) == 2
        assert "SPY" in feed._contracts
        assert "QQQ" in feed._contracts
        assert len(feed._tickers) == 2

    async def test_start_disconnected(self):
        """Test start fails if IB not connected."""
        mock_ib = MockIB()
        mock_ib.connected = False
        feed = IBDataFeed(mock_ib, symbols=["SPY"])

        with pytest.raises(RuntimeError, match="IB must be connected"):
            await feed.start()

    async def test_stop(self):
        """Test feed stop."""
        mock_ib = MockIB()
        feed = IBDataFeed(mock_ib, symbols=["SPY"])

        await feed.start()
        assert feed._running

        feed.stop()

        assert not feed._running
        # Check queue got None sentinel
        sentinel = await asyncio.wait_for(feed._queue.get(), timeout=1.0)
        assert sentinel is None

    async def test_tick_emission(self):
        """Test that ticks are emitted to queue."""
        mock_ib = MockIB()
        feed = IBDataFeed(mock_ib, symbols=["SPY"])

        await feed.start()

        # Create mock ticker with data
        ticker = MockTicker(Stock("SPY", "SMART", "USD"))
        ticker.last = 450.0
        ticker.lastSize = 100
        ticker.bid = 449.99
        ticker.ask = 450.01
        ticker.bidSize = 200
        ticker.askSize = 150
        ticker.volume = 1000000

        # Emit tick
        mock_ib.pendingTickersEvent.emit([ticker])

        # Wait briefly for processing
        await asyncio.sleep(0.05)

        # Check data was queued
        item = await asyncio.wait_for(feed._queue.get(), timeout=1.0)
        assert item is not None

        assert item.kind is MarketEventKind.TRADE
        assert item.asset == "SPY"
        assert item.payload == TradePayload(450.0, 100.0)
        assert item.event_time.tzinfo is not None
        assert item.metadata["event_time_capability"].startswith("local receipt")
        quote = await asyncio.wait_for(feed._queue.get(), timeout=1.0)
        assert quote is not None
        assert quote.kind is MarketEventKind.QUOTE
        assert quote.payload == QuotePayload(449.99, 450.01, 200.0, 150.0)
        assert quote.metadata["volume"] == 1_000_000.0

        feed.stop()

    async def test_tick_throttling(self):
        """Test tick throttling."""
        mock_ib = MockIB()
        feed = IBDataFeed(mock_ib, symbols=["SPY"], tick_throttle_ms=100)

        await feed.start()

        ticker = MockTicker(Stock("SPY", "SMART", "USD"))
        ticker.last = 450.0
        ticker.lastSize = 100

        # Emit two ticks rapidly
        mock_ib.pendingTickersEvent.emit([ticker])
        await asyncio.sleep(0.01)  # 10ms < 100ms throttle
        mock_ib.pendingTickersEvent.emit([ticker])

        await asyncio.sleep(0.05)

        # Only one tick should have been emitted due to throttling
        queued_count = 0
        try:
            while True:
                await asyncio.wait_for(feed._queue.get(), timeout=0.1)
                queued_count += 1
        except TimeoutError:
            pass

        assert queued_count == 1
        assert feed._throttled_count > 0

        feed.stop()

    async def test_skip_invalid_ticks(self):
        """Test that ticks with no price are skipped."""
        mock_ib = MockIB()
        feed = IBDataFeed(mock_ib, symbols=["SPY"])

        await feed.start()

        # Ticker with no price
        ticker = MockTicker(Stock("SPY", "SMART", "USD"))
        ticker.last = None

        mock_ib.pendingTickersEvent.emit([ticker])
        await asyncio.sleep(0.05)

        # No data should be queued
        try:
            await asyncio.wait_for(feed._queue.get(), timeout=0.1)
            assert False, "Should not have queued invalid tick"
        except TimeoutError:
            pass  # Expected

        feed.stop()

    async def test_naive_provider_time_is_rejected(self):
        mock_ib = MockIB()
        feed = IBDataFeed(mock_ib, symbols=["SPY"], tick_throttle_ms=0)
        await feed.start()
        ticker = MockTicker(Stock("SPY", "SMART", "USD"))
        ticker.last = 450.0
        ticker.lastSize = 100
        ticker.time = datetime(2024, 1, 1)

        mock_ib.pendingTickersEvent.emit([ticker])

        assert feed._queue.empty()
        assert feed.stats["rejected_count"] == 1
        feed.stop()

    async def test_crossed_quote_is_rejected_without_dropping_valid_trade(self):
        mock_ib = MockIB()
        feed = IBDataFeed(mock_ib, symbols=["SPY"], tick_throttle_ms=0)
        await feed.start()
        ticker = MockTicker(Stock("SPY", "SMART", "USD"))
        ticker.last = 450.0
        ticker.lastSize = 100
        ticker.bid = 451.0
        ticker.ask = 450.0
        ticker.bidSize = 10
        ticker.askSize = 10
        ticker.time = datetime.now(UTC) - timedelta(milliseconds=1)

        mock_ib.pendingTickersEvent.emit([ticker])

        event = feed._queue.get_nowait()
        assert event is not None
        assert event.kind is MarketEventKind.TRADE
        assert feed._queue.empty()
        assert feed.stats["rejected_count"] == 1
        feed.stop()

    async def test_restart_does_not_consume_previous_shutdown_sentinel(self):
        mock_ib = MockIB()
        feed = IBDataFeed(mock_ib, symbols=["SPY"])
        await feed.start()
        feed.stop()

        await feed.start()
        ticker = MockTicker(Stock("SPY", "SMART", "USD"))
        ticker.last = 450.0
        ticker.lastSize = 100
        mock_ib.pendingTickersEvent.emit([ticker])

        event = await asyncio.wait_for(anext(feed.__aiter__()), timeout=1.0)
        assert event.kind is MarketEventKind.TRADE
        feed.stop()

    async def test_multiple_symbols(self):
        """Test feed with multiple symbols."""
        mock_ib = MockIB()
        feed = IBDataFeed(mock_ib, symbols=["SPY", "QQQ", "IWM"])

        await feed.start()

        # Create tickers for all symbols
        tickers = [
            MockTicker(Stock("SPY", "SMART", "USD")),
            MockTicker(Stock("QQQ", "SMART", "USD")),
            MockTicker(Stock("IWM", "SMART", "USD")),
        ]
        for i, ticker in enumerate(tickers):
            ticker.last = 100.0 + i
            ticker.lastSize = 100

        # Emit all tickers
        mock_ib.pendingTickersEvent.emit(tickers)
        await asyncio.sleep(0.05)

        events = [await asyncio.wait_for(feed._queue.get(), timeout=1.0) for _ in range(3)]
        assert {event.asset for event in events if event is not None} == {"SPY", "QQQ", "IWM"}
        assert all(event is not None and event.kind is MarketEventKind.TRADE for event in events)

        feed.stop()

    async def test_async_iteration(self):
        """Test async iteration over feed."""
        mock_ib = MockIB()
        feed = IBDataFeed(mock_ib, symbols=["SPY"])

        await feed.start()

        # Emit a tick
        ticker = MockTicker(Stock("SPY", "SMART", "USD"))
        ticker.last = 450.0
        ticker.lastSize = 100
        mock_ib.pendingTickersEvent.emit([ticker])

        # Iterate
        received = []

        async def collect_data():
            async for event in feed:
                received.append(event)
                if len(received) >= 1:
                    break

        await asyncio.wait_for(collect_data(), timeout=1.0)

        assert len(received) == 1
        assert received[0].asset == "SPY"
        assert received[0].payload == TradePayload(450.0, 100.0)

        feed.stop()

    async def test_stats(self):
        """Test feed statistics."""
        mock_ib = MockIB()
        feed = IBDataFeed(mock_ib, symbols=["SPY"])

        await feed.start()

        # Emit some ticks
        ticker = MockTicker(Stock("SPY", "SMART", "USD"))
        ticker.last = 450.0
        ticker.lastSize = 100

        for i in range(5):
            mock_ib.pendingTickersEvent.emit([ticker])
            await asyncio.sleep(0.15)  # Allow throttle to pass

        await asyncio.sleep(0.2)

        stats = feed.stats
        assert stats["running"]
        assert stats["tick_count"] >= 1
        assert stats["symbols"] == ["SPY"]

        feed.stop()

    async def test_stop_during_iteration(self):
        """Test that stop() terminates iteration cleanly."""
        mock_ib = MockIB()
        feed = IBDataFeed(mock_ib, symbols=["SPY"])

        await feed.start()

        # Create background task that emits ticks
        async def emit_ticks():
            ticker = MockTicker(Stock("SPY", "SMART", "USD"))
            ticker.last = 450.0
            ticker.lastSize = 100
            for _ in range(100):
                mock_ib.pendingTickersEvent.emit([ticker])
                await asyncio.sleep(0.2)

        emit_task = asyncio.create_task(emit_ticks())

        # Collect a few items then stop
        count = 0
        async for event in feed:
            count += 1
            if count >= 2:
                feed.stop()
                break

        assert count >= 2

        # Clean up
        emit_task.cancel()
        try:
            await emit_task
        except asyncio.CancelledError:
            pass
