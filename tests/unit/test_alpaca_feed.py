"""Unit tests for AlpacaDataFeed."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from ml4t.live.feeds.alpaca_feed import AlpacaDataFeed


class MockAlpacaBar:
    """Mock Alpaca Bar object."""

    def __init__(
        self,
        symbol: str = "AAPL",
        open: float = 150.0,
        high: float = 152.0,
        low: float = 149.0,
        close: float = 151.0,
        volume: int = 1000000,
        timestamp: datetime | None = None,
        vwap: float | None = 150.5,
        trade_count: int | None = 5000,
    ):
        self.symbol = symbol
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume
        self.timestamp = timestamp or datetime.now(timezone.utc)
        self.vwap = vwap
        self.trade_count = trade_count


class MockAlpacaQuote:
    """Mock Alpaca Quote object."""

    def __init__(
        self,
        symbol: str = "AAPL",
        bid_price: float = 150.0,
        ask_price: float = 150.10,
        bid_size: int = 100,
        ask_size: int = 150,
        timestamp: datetime | None = None,
    ):
        self.symbol = symbol
        self.bid_price = bid_price
        self.ask_price = ask_price
        self.bid_size = bid_size
        self.ask_size = ask_size
        self.timestamp = timestamp or datetime.now(timezone.utc)


class MockAlpacaTrade:
    """Mock Alpaca Trade object."""

    def __init__(
        self,
        symbol: str = "AAPL",
        price: float = 150.05,
        size: int = 100,
        exchange: str = "XNAS",
        conditions: list[str] | None = None,
        timestamp: datetime | None = None,
    ):
        self.symbol = symbol
        self.price = price
        self.size = size
        self.exchange = exchange
        self.conditions = conditions or ["@"]
        self.timestamp = timestamp or datetime.now(timezone.utc)


class MockCryptoTrade:
    """Mock Alpaca Crypto Trade object."""

    def __init__(
        self,
        symbol: str = "BTC/USD",
        price: float = 43000.0,
        size: float = 0.5,
        taker_side: str = "buy",
        timestamp: datetime | None = None,
    ):
        self.symbol = symbol
        self.price = price
        self.size = size
        self.taker_side = taker_side
        self.timestamp = timestamp or datetime.now(timezone.utc)


class TestAlpacaDataFeedSetup:
    """Test suite for AlpacaDataFeed initialization."""

    def test_initialization_defaults(self):
        """Test AlpacaDataFeed initialization with defaults."""
        feed = AlpacaDataFeed(
            api_key="PKTEST123",
            secret_key="SECRETTEST",
            symbols=["AAPL", "MSFT"],
        )

        assert feed._api_key == "PKTEST123"
        assert feed._secret_key == "SECRETTEST"
        assert feed._data_type == "bars"  # Default
        assert feed._feed == "iex"  # Default
        assert feed._stock_symbols == ["AAPL", "MSFT"]
        assert feed._crypto_symbols == []
        assert feed._running is False

    def test_initialization_with_crypto(self):
        """Test AlpacaDataFeed initialization with crypto symbols."""
        feed = AlpacaDataFeed(
            api_key="PKTEST123",
            secret_key="SECRETTEST",
            symbols=["AAPL", "BTC/USD", "ETH/USD"],
        )

        assert feed._stock_symbols == ["AAPL"]
        assert feed._crypto_symbols == ["BTC/USD", "ETH/USD"]

    def test_initialization_only_crypto(self):
        """Test AlpacaDataFeed with only crypto symbols."""
        feed = AlpacaDataFeed(
            api_key="PKTEST123",
            secret_key="SECRETTEST",
            symbols=["BTC/USD", "ETH/USD", "DOGE/USD"],
        )

        assert feed._stock_symbols == []
        assert feed._crypto_symbols == ["BTC/USD", "ETH/USD", "DOGE/USD"]

    def test_initialization_quotes(self):
        """Test AlpacaDataFeed with quotes data type."""
        feed = AlpacaDataFeed(
            api_key="PKTEST123",
            secret_key="SECRETTEST",
            symbols=["AAPL"],
            data_type="quotes",
        )

        assert feed._data_type == "quotes"

    def test_initialization_trades(self):
        """Test AlpacaDataFeed with trades data type."""
        feed = AlpacaDataFeed(
            api_key="PKTEST123",
            secret_key="SECRETTEST",
            symbols=["AAPL"],
            data_type="trades",
        )

        assert feed._data_type == "trades"

    def test_initialization_sip_feed(self):
        """Test AlpacaDataFeed with SIP premium feed."""
        feed = AlpacaDataFeed(
            api_key="PKTEST123",
            secret_key="SECRETTEST",
            symbols=["AAPL"],
            feed="sip",
        )

        assert feed._feed == "sip"


class TestAlpacaDataFeedCryptoDetection:
    """Test suite for crypto symbol detection."""

    def test_is_crypto_btc(self):
        """Test BTC/USD is detected as crypto."""
        feed = AlpacaDataFeed(
            api_key="PKTEST",
            secret_key="SECRET",
            symbols=[],
        )

        assert feed._is_crypto("BTC/USD") is True

    def test_is_crypto_eth(self):
        """Test ETH/USD is detected as crypto."""
        feed = AlpacaDataFeed(
            api_key="PKTEST",
            secret_key="SECRET",
            symbols=[],
        )

        assert feed._is_crypto("ETH/USD") is True

    def test_is_crypto_stock(self):
        """Test AAPL is not detected as crypto."""
        feed = AlpacaDataFeed(
            api_key="PKTEST",
            secret_key="SECRET",
            symbols=[],
        )

        assert feed._is_crypto("AAPL") is False

    def test_is_crypto_lowercase(self):
        """Test lowercase handling."""
        feed = AlpacaDataFeed(
            api_key="PKTEST",
            secret_key="SECRET",
            symbols=[],
        )

        assert feed._is_crypto("btc/usd") is True


class TestAlpacaDataFeedStart:
    """Test suite for starting the feed."""

    @pytest.mark.asyncio
    @patch("ml4t.live.feeds.alpaca_feed.StockDataStream")
    async def test_start_stocks_only(self, mock_stream_class):
        """Test starting feed with only stock symbols."""
        mock_stream = MagicMock()
        mock_stream_class.return_value = mock_stream

        feed = AlpacaDataFeed(
            api_key="PKTEST",
            secret_key="SECRET",
            symbols=["AAPL", "MSFT"],
        )

        await feed.start()

        assert feed._running is True
        mock_stream_class.assert_called_once()
        mock_stream.subscribe_bars.assert_called_once()

    @pytest.mark.asyncio
    @patch("ml4t.live.feeds.alpaca_feed.CryptoDataStream")
    async def test_start_crypto_only(self, mock_stream_class):
        """Test starting feed with only crypto symbols."""
        mock_stream = MagicMock()
        mock_stream_class.return_value = mock_stream

        feed = AlpacaDataFeed(
            api_key="PKTEST",
            secret_key="SECRET",
            symbols=["BTC/USD", "ETH/USD"],
        )

        await feed.start()

        assert feed._running is True
        mock_stream_class.assert_called_once()
        mock_stream.subscribe_bars.assert_called_once()

    @pytest.mark.asyncio
    @patch("ml4t.live.feeds.alpaca_feed.StockDataStream")
    @patch("ml4t.live.feeds.alpaca_feed.CryptoDataStream")
    async def test_start_mixed_symbols(self, mock_crypto_class, mock_stock_class):
        """Test starting feed with both stock and crypto symbols."""
        mock_stock = MagicMock()
        mock_stock_class.return_value = mock_stock
        mock_crypto = MagicMock()
        mock_crypto_class.return_value = mock_crypto

        feed = AlpacaDataFeed(
            api_key="PKTEST",
            secret_key="SECRET",
            symbols=["AAPL", "BTC/USD"],
        )

        await feed.start()

        assert feed._running is True
        mock_stock_class.assert_called_once()
        mock_crypto_class.assert_called_once()
        mock_stock.subscribe_bars.assert_called_once()
        mock_crypto.subscribe_bars.assert_called_once()

    @pytest.mark.asyncio
    @patch("ml4t.live.feeds.alpaca_feed.StockDataStream")
    async def test_start_with_quotes(self, mock_stream_class):
        """Test starting feed with quotes data type."""
        mock_stream = MagicMock()
        mock_stream_class.return_value = mock_stream

        feed = AlpacaDataFeed(
            api_key="PKTEST",
            secret_key="SECRET",
            symbols=["AAPL"],
            data_type="quotes",
        )

        await feed.start()

        mock_stream.subscribe_quotes.assert_called_once()

    @pytest.mark.asyncio
    @patch("ml4t.live.feeds.alpaca_feed.StockDataStream")
    async def test_start_with_trades(self, mock_stream_class):
        """Test starting feed with trades data type."""
        mock_stream = MagicMock()
        mock_stream_class.return_value = mock_stream

        feed = AlpacaDataFeed(
            api_key="PKTEST",
            secret_key="SECRET",
            symbols=["AAPL"],
            data_type="trades",
        )

        await feed.start()

        mock_stream.subscribe_trades.assert_called_once()


class TestAlpacaDataFeedStop:
    """Test suite for stopping the feed."""

    def test_stop(self):
        """Test stopping the feed."""
        feed = AlpacaDataFeed(
            api_key="PKTEST",
            secret_key="SECRET",
            symbols=["AAPL"],
        )
        feed._running = True
        feed._stock_stream = MagicMock()

        feed.stop()

        assert feed._running is False
        feed._stock_stream.stop.assert_called_once()

    def test_stop_with_crypto_stream(self):
        """Test stopping the feed with crypto stream."""
        feed = AlpacaDataFeed(
            api_key="PKTEST",
            secret_key="SECRET",
            symbols=["BTC/USD"],
        )
        feed._running = True
        feed._crypto_stream = MagicMock()

        feed.stop()

        assert feed._running is False
        feed._crypto_stream.stop.assert_called_once()

    def test_stop_when_not_running(self):
        """Test stopping when not running is safe."""
        feed = AlpacaDataFeed(
            api_key="PKTEST",
            secret_key="SECRET",
            symbols=["AAPL"],
        )
        feed._running = False

        # Should not raise
        feed.stop()


class TestAlpacaDataFeedHandlers:
    """Test suite for data handlers."""

    @pytest.mark.asyncio
    async def test_on_stock_bar(self):
        """Test stock bar handler."""
        feed = AlpacaDataFeed(
            api_key="PKTEST",
            secret_key="SECRET",
            symbols=["AAPL"],
        )
        feed._running = True

        bar = MockAlpacaBar(
            symbol="AAPL",
            open=150.0,
            high=152.0,
            low=149.0,
            close=151.0,
            volume=1000000,
        )

        await feed._on_stock_bar(bar)

        # Check data was queued
        assert feed._queue.qsize() == 1
        timestamp, data, context = await feed._queue.get()

        assert "AAPL" in data
        assert data["AAPL"]["open"] == 150.0
        assert data["AAPL"]["high"] == 152.0
        assert data["AAPL"]["low"] == 149.0
        assert data["AAPL"]["close"] == 151.0
        assert data["AAPL"]["volume"] == 1000000
        assert feed._bar_count == 1

    @pytest.mark.asyncio
    async def test_on_stock_bar_not_running(self):
        """Test stock bar handler when not running."""
        feed = AlpacaDataFeed(
            api_key="PKTEST",
            secret_key="SECRET",
            symbols=["AAPL"],
        )
        feed._running = False

        bar = MockAlpacaBar(symbol="AAPL")

        await feed._on_stock_bar(bar)

        # No data should be queued
        assert feed._queue.qsize() == 0

    @pytest.mark.asyncio
    async def test_on_stock_quote(self):
        """Test stock quote handler."""
        feed = AlpacaDataFeed(
            api_key="PKTEST",
            secret_key="SECRET",
            symbols=["AAPL"],
            data_type="quotes",
        )
        feed._running = True

        quote = MockAlpacaQuote(
            symbol="AAPL",
            bid_price=150.0,
            ask_price=150.10,
            bid_size=100,
            ask_size=150,
        )

        await feed._on_stock_quote(quote)

        assert feed._queue.qsize() == 1
        timestamp, data, context = await feed._queue.get()

        assert "AAPL" in data
        assert data["AAPL"]["bid"] == 150.0
        assert data["AAPL"]["ask"] == 150.10
        assert data["AAPL"]["price"] == 150.05  # Mid price
        assert feed._quote_count == 1

    @pytest.mark.asyncio
    async def test_on_stock_trade(self):
        """Test stock trade handler."""
        feed = AlpacaDataFeed(
            api_key="PKTEST",
            secret_key="SECRET",
            symbols=["AAPL"],
            data_type="trades",
        )
        feed._running = True

        trade = MockAlpacaTrade(
            symbol="AAPL",
            price=150.05,
            size=100,
            exchange="XNAS",
        )

        await feed._on_stock_trade(trade)

        assert feed._queue.qsize() == 1
        timestamp, data, context = await feed._queue.get()

        assert "AAPL" in data
        assert data["AAPL"]["price"] == 150.05
        assert data["AAPL"]["size"] == 100
        assert feed._trade_count == 1

    @pytest.mark.asyncio
    async def test_on_crypto_bar(self):
        """Test crypto bar handler."""
        feed = AlpacaDataFeed(
            api_key="PKTEST",
            secret_key="SECRET",
            symbols=["BTC/USD"],
        )
        feed._running = True

        bar = MockAlpacaBar(
            symbol="BTC/USD",
            open=43000.0,
            high=43500.0,
            low=42800.0,
            close=43200.0,
            volume=500,  # Crypto volume is typically lower in units
        )

        await feed._on_crypto_bar(bar)

        assert feed._queue.qsize() == 1
        timestamp, data, context = await feed._queue.get()

        assert "BTC/USD" in data
        assert data["BTC/USD"]["open"] == 43000.0
        assert data["BTC/USD"]["close"] == 43200.0


class TestAlpacaDataFeedIteration:
    """Test suite for async iteration."""

    @pytest.mark.asyncio
    async def test_aiter(self):
        """Test async iteration yields queued data."""
        feed = AlpacaDataFeed(
            api_key="PKTEST",
            secret_key="SECRET",
            symbols=["AAPL"],
        )
        feed._running = True

        # Queue some data
        timestamp = datetime.now(timezone.utc)
        data = {"AAPL": {"close": 150.0}}
        context = {}
        feed._queue.put_nowait((timestamp, data, context))

        # Queue stop signal
        feed._queue.put_nowait(None)

        # Iterate
        results = []
        async for item in feed:
            results.append(item)

        assert len(results) == 1
        assert results[0][1] == {"AAPL": {"close": 150.0}}

    @pytest.mark.asyncio
    async def test_anext(self):
        """Test async next returns queued data."""
        feed = AlpacaDataFeed(
            api_key="PKTEST",
            secret_key="SECRET",
            symbols=["AAPL"],
        )
        feed._running = True

        timestamp = datetime.now(timezone.utc)
        data = {"AAPL": {"close": 150.0}}
        context = {}
        feed._queue.put_nowait((timestamp, data, context))

        result = await feed.__anext__()

        assert result[1] == {"AAPL": {"close": 150.0}}

    @pytest.mark.asyncio
    async def test_anext_not_running(self):
        """Test async next raises StopAsyncIteration when not running."""
        feed = AlpacaDataFeed(
            api_key="PKTEST",
            secret_key="SECRET",
            symbols=["AAPL"],
        )
        feed._running = False

        with pytest.raises(StopAsyncIteration):
            await feed.__anext__()

    @pytest.mark.asyncio
    async def test_anext_stop_signal(self):
        """Test async next raises StopAsyncIteration on None signal."""
        feed = AlpacaDataFeed(
            api_key="PKTEST",
            secret_key="SECRET",
            symbols=["AAPL"],
        )
        feed._running = True
        feed._queue.put_nowait(None)

        with pytest.raises(StopAsyncIteration):
            await feed.__anext__()


class TestAlpacaDataFeedStats:
    """Test suite for stats property."""

    def test_stats_initial(self):
        """Test initial stats."""
        feed = AlpacaDataFeed(
            api_key="PKTEST",
            secret_key="SECRET",
            symbols=["AAPL", "BTC/USD"],
        )

        stats = feed.stats

        assert stats["running"] is False
        assert stats["bar_count"] == 0
        assert stats["quote_count"] == 0
        assert stats["trade_count"] == 0
        assert stats["stock_symbols"] == ["AAPL"]
        assert stats["crypto_symbols"] == ["BTC/USD"]
        assert stats["data_type"] == "bars"
        assert stats["feed"] == "iex"

    @pytest.mark.asyncio
    async def test_stats_after_bars(self):
        """Test stats after receiving bars."""
        feed = AlpacaDataFeed(
            api_key="PKTEST",
            secret_key="SECRET",
            symbols=["AAPL"],
        )
        feed._running = True

        # Process some bars
        for i in range(5):
            await feed._on_stock_bar(MockAlpacaBar(symbol="AAPL"))

        stats = feed.stats

        assert stats["bar_count"] == 5
