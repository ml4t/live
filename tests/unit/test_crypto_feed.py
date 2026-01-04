"""Unit tests for CryptoFeed."""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from ml4t.live.feeds.crypto_feed import CryptoFeed


class MockExchange:
    """Mock CCXT exchange."""

    def __init__(self, exchange_id="binance"):
        self.id = exchange_id
        self.markets = {
            "BTC/USDT": {"id": "BTCUSDT", "symbol": "BTC/USDT"},
            "ETH/USDT": {"id": "ETHUSDT", "symbol": "ETH/USDT"},
        }

    async def load_markets(self):
        """Mock load_markets."""
        pass

    async def close(self):
        """Mock close."""
        pass


@pytest.mark.asyncio
class TestCryptoFeed:
    """Test suite for CryptoFeed.

    Note: These tests use mocks and don't require actual ccxt package.
    """

    async def test_initialization_mock(self):
        """Test CryptoFeed initialization with mock."""
        with patch("ml4t.live.feeds.crypto_feed.CCXT_AVAILABLE", True):
            with patch("ml4t.live.feeds.crypto_feed.ccxt") as mock_ccxt:
                mock_exchange = MockExchange()
                mock_ccxt.binance.return_value = mock_exchange

                feed = CryptoFeed(
                    exchange="binance",
                    symbols=["BTC/USDT", "ETH/USDT"],
                    timeframe="1m",
                )

                assert feed.exchange_id == "binance"
                assert feed.symbols == ["BTC/USDT", "ETH/USDT"]
                assert feed.timeframe == "1m"
                assert not feed.stream_trades
                assert feed.stream_ohlcv
                assert not feed._running

    async def test_with_api_credentials_mock(self):
        """Test initialization with API credentials with mock."""
        with patch("ml4t.live.feeds.crypto_feed.CCXT_AVAILABLE", True):
            with patch("ml4t.live.feeds.crypto_feed.ccxt") as mock_ccxt:
                mock_exchange = MockExchange()
                mock_ccxt.binance.return_value = mock_exchange

                feed = CryptoFeed(
                    exchange="binance",
                    symbols=["BTC/USDT"],
                    api_key="test-key",
                    api_secret="test-secret",
                )

                # Verify config was passed
                assert feed.exchange_id == "binance"
                assert mock_ccxt.binance.called
                call_config = mock_ccxt.binance.call_args[0][0]
                assert call_config["apiKey"] == "test-key"
                assert call_config["secret"] == "test-secret"

    async def test_process_trade_mock(self):
        """Test processing trade data with mock."""
        with patch("ml4t.live.feeds.crypto_feed.CCXT_AVAILABLE", True):
            with patch("ml4t.live.feeds.crypto_feed.ccxt") as mock_ccxt:
                mock_exchange = MockExchange()
                mock_ccxt.binance.return_value = mock_exchange

                feed = CryptoFeed(
                    exchange="binance",
                    symbols=["BTC/USDT"],
                )

                trade = {
                    "timestamp": int(datetime(2024, 1, 1, 10, 0, 0).timestamp() * 1000),
                    "price": 50000.0,
                    "amount": 0.5,
                    "side": "buy",
                    "id": "12345",
                }

                await feed._process_trade(trade, "BTC/USDT")

                # Check data was queued
                item = await asyncio.wait_for(feed._queue.get(), timeout=1.0)
                timestamp, data, context = item

                assert "BTC/USDT" in data
                assert data["BTC/USDT"]["price"] == 50000.0
                assert data["BTC/USDT"]["size"] == 0.5
                assert context["BTC/USDT"]["side"] == "buy"
                assert context["BTC/USDT"]["trade_id"] == "12345"

    async def test_process_candle_mock(self):
        """Test processing OHLCV candle with mock."""
        with patch("ml4t.live.feeds.crypto_feed.CCXT_AVAILABLE", True):
            with patch("ml4t.live.feeds.crypto_feed.ccxt") as mock_ccxt:
                mock_exchange = MockExchange()
                mock_ccxt.binance.return_value = mock_exchange

                feed = CryptoFeed(
                    exchange="binance",
                    symbols=["BTC/USDT"],
                    timeframe="1m",
                )

                candle = [
                    int(datetime(2024, 1, 1, 10, 0, 0).timestamp() * 1000),  # timestamp
                    50000.0,  # open
                    51000.0,  # high
                    49000.0,  # low
                    50500.0,  # close
                    10.5,  # volume
                ]

                await feed._process_candle(candle, "BTC/USDT")

                # Check data was queued
                item = await asyncio.wait_for(feed._queue.get(), timeout=1.0)
                timestamp, data, context = item

                assert "BTC/USDT" in data
                assert data["BTC/USDT"]["open"] == 50000.0
                assert data["BTC/USDT"]["high"] == 51000.0
                assert data["BTC/USDT"]["low"] == 49000.0
                assert data["BTC/USDT"]["close"] == 50500.0
                assert data["BTC/USDT"]["volume"] == 10.5
                assert context["BTC/USDT"]["timeframe"] == "1m"
                assert context["BTC/USDT"]["exchange"] == "binance"

    async def test_stats_mock(self):
        """Test feed statistics with mock."""
        with patch("ml4t.live.feeds.crypto_feed.CCXT_AVAILABLE", True):
            with patch("ml4t.live.feeds.crypto_feed.ccxt") as mock_ccxt:
                mock_exchange = MockExchange()
                mock_ccxt.binance.return_value = mock_exchange

                feed = CryptoFeed(
                    exchange="binance",
                    symbols=["BTC/USDT", "ETH/USDT"],
                    timeframe="5m",
                )

                # Process some data
                trade = {
                    "timestamp": int(datetime.now().timestamp() * 1000),
                    "price": 50000.0,
                    "amount": 0.1,
                    "side": "buy",
                    "id": "1",
                }
                await feed._process_trade(trade, "BTC/USDT")

                candle = [int(datetime.now().timestamp() * 1000), 50000, 51000, 49000, 50500, 10]
                await feed._process_candle(candle, "ETH/USDT")

                stats = feed.stats

                assert stats["exchange"] == "binance"
                assert stats["symbols"] == ["BTC/USDT", "ETH/USDT"]
                assert stats["timeframe"] == "5m"
                assert stats["tick_count"] == 2
                assert stats["trade_count"] == 1
                assert stats["candle_count"] == 1

    async def test_missing_dependency(self):
        """Test error when ccxt not installed."""
        with patch("ml4t.live.feeds.crypto_feed.CCXT_AVAILABLE", False):
            with pytest.raises(ImportError, match="ccxt package required"):
                CryptoFeed(
                    exchange="binance",
                    symbols=["BTC/USDT"],
                )

    async def test_async_iteration_mock(self):
        """Test async iteration over feed with mock."""
        with patch("ml4t.live.feeds.crypto_feed.CCXT_AVAILABLE", True):
            with patch("ml4t.live.feeds.crypto_feed.ccxt") as mock_ccxt:
                mock_exchange = MockExchange()
                mock_ccxt.binance.return_value = mock_exchange

                feed = CryptoFeed(
                    exchange="binance",
                    symbols=["BTC/USDT"],
                )

                # Queue some data directly
                ts = datetime.now()
                data = {
                    "BTC/USDT": {
                        "open": 50000,
                        "high": 51000,
                        "low": 49000,
                        "close": 50500,
                        "volume": 10,
                    }
                }
                context = {"BTC/USDT": {"timeframe": "1m"}}
                feed._running = True  # Enable iteration
                feed._queue.put_nowait((ts, data, context))
                feed._queue.put_nowait(None)  # Sentinel

                items = []
                async for item in feed:
                    items.append(item)

                assert len(items) == 1
                assert items[0][1]["BTC/USDT"]["close"] == 50500

    async def test_close_mock(self):
        """Test close method with mock."""
        with patch("ml4t.live.feeds.crypto_feed.CCXT_AVAILABLE", True):
            with patch("ml4t.live.feeds.crypto_feed.ccxt") as mock_ccxt:
                mock_exchange = MockExchange()
                mock_exchange.close = AsyncMock()
                mock_ccxt.binance.return_value = mock_exchange

                feed = CryptoFeed(
                    exchange="binance",
                    symbols=["BTC/USDT"],
                )

                await feed.close()

                mock_exchange.close.assert_called_once()

    async def test_start_method(self):
        """Test start method initiates streaming."""
        with patch("ml4t.live.feeds.crypto_feed.CCXT_AVAILABLE", True):
            with patch("ml4t.live.feeds.crypto_feed.ccxt") as mock_ccxt:
                mock_exchange = MockExchange()
                mock_exchange.load_markets = AsyncMock()
                mock_ccxt.binance.return_value = mock_exchange

                feed = CryptoFeed(
                    exchange="binance",
                    symbols=["BTC/USDT"],
                    stream_ohlcv=True,
                    stream_trades=True,
                )

                # Start and immediately stop
                await feed.start()

                assert feed._running is True
                mock_exchange.load_markets.assert_called_once()
                # Should create 2 tasks (1 ohlcv + 1 trade per symbol)
                assert len(feed._stream_tasks) == 2

                # Clean up
                feed.stop()

    async def test_stop_method(self):
        """Test stop method cancels tasks and signals queue."""
        with patch("ml4t.live.feeds.crypto_feed.CCXT_AVAILABLE", True):
            with patch("ml4t.live.feeds.crypto_feed.ccxt") as mock_ccxt:
                mock_exchange = MockExchange()
                mock_exchange.load_markets = AsyncMock()
                mock_ccxt.binance.return_value = mock_exchange

                feed = CryptoFeed(
                    exchange="binance",
                    symbols=["BTC/USDT"],
                )

                # Start feed
                await feed.start()
                assert feed._running is True

                # Stop feed
                feed.stop()

                assert feed._running is False
                # Queue should have None sentinel
                assert feed._queue.get_nowait() is None

    async def test_stream_trades_websocket(self):
        """Test streaming trades via WebSocket."""
        with patch("ml4t.live.feeds.crypto_feed.CCXT_AVAILABLE", True):
            with patch("ml4t.live.feeds.crypto_feed.ccxt") as mock_ccxt:
                mock_exchange = MockExchange()
                mock_exchange.load_markets = AsyncMock()

                # Mock watch_trades to return trades then raise CancelledError
                trade_data = [
                    {
                        "timestamp": 1704067200000,
                        "price": 50000,
                        "amount": 1.5,
                        "side": "buy",
                        "id": "T1",
                    }
                ]
                call_count = [0]

                async def mock_watch_trades(symbol):
                    call_count[0] += 1
                    if call_count[0] > 1:
                        raise asyncio.CancelledError()
                    return trade_data

                mock_exchange.watch_trades = mock_watch_trades
                mock_ccxt.binance.return_value = mock_exchange

                feed = CryptoFeed(
                    exchange="binance",
                    symbols=["BTC/USDT"],
                    stream_trades=True,
                    stream_ohlcv=False,
                )
                feed._running = True

                # Run stream (will be cancelled after first iteration)
                await feed._stream_trades_for_symbol("BTC/USDT")

                # Should have processed one trade
                assert feed._trade_count == 1

    async def test_stream_trades_rest_fallback(self):
        """Test streaming trades via REST polling fallback."""
        with patch("ml4t.live.feeds.crypto_feed.CCXT_AVAILABLE", True):
            with patch("ml4t.live.feeds.crypto_feed.ccxt") as mock_ccxt:
                mock_exchange = MockExchange()
                # No watch_trades = REST fallback

                trade_data = [
                    {
                        "timestamp": 1704067200000,
                        "price": 50000,
                        "amount": 1.5,
                        "side": "buy",
                        "id": "T1",
                    }
                ]
                call_count = [0]

                async def mock_fetch_trades(symbol, limit=100):
                    call_count[0] += 1
                    if call_count[0] > 1:
                        raise asyncio.CancelledError()
                    return trade_data

                mock_exchange.fetch_trades = mock_fetch_trades
                mock_ccxt.binance.return_value = mock_exchange

                feed = CryptoFeed(
                    exchange="binance",
                    symbols=["BTC/USDT"],
                    stream_trades=True,
                    stream_ohlcv=False,
                )
                feed._running = True

                await feed._stream_trades_for_symbol("BTC/USDT")

                assert feed._trade_count == 1

    async def test_stream_ohlcv_websocket(self):
        """Test streaming OHLCV via WebSocket."""
        with patch("ml4t.live.feeds.crypto_feed.CCXT_AVAILABLE", True):
            with patch("ml4t.live.feeds.crypto_feed.ccxt") as mock_ccxt:
                mock_exchange = MockExchange()
                mock_exchange.load_markets = AsyncMock()

                # OHLCV format: [timestamp, open, high, low, close, volume]
                candle_data = [[1704067200000, 50000, 51000, 49000, 50500, 100.5]]
                call_count = [0]

                async def mock_watch_ohlcv(symbol, timeframe):
                    call_count[0] += 1
                    if call_count[0] > 1:
                        raise asyncio.CancelledError()
                    return candle_data

                mock_exchange.watch_ohlcv = mock_watch_ohlcv
                mock_ccxt.binance.return_value = mock_exchange

                feed = CryptoFeed(
                    exchange="binance",
                    symbols=["BTC/USDT"],
                    stream_ohlcv=True,
                )
                feed._running = True

                await feed._stream_ohlcv_for_symbol("BTC/USDT")

                assert feed._candle_count == 1

    async def test_stream_ohlcv_rest_fallback(self):
        """Test streaming OHLCV via REST polling fallback."""
        with patch("ml4t.live.feeds.crypto_feed.CCXT_AVAILABLE", True):
            with patch("ml4t.live.feeds.crypto_feed.ccxt") as mock_ccxt:
                mock_exchange = MockExchange()
                # No watch_ohlcv = REST fallback

                candle_data = [[1704067200000, 50000, 51000, 49000, 50500, 100.5]]
                call_count = [0]

                async def mock_fetch_ohlcv(symbol, timeframe, limit=2):
                    call_count[0] += 1
                    if call_count[0] > 1:
                        raise asyncio.CancelledError()
                    return candle_data

                mock_exchange.fetch_ohlcv = mock_fetch_ohlcv
                mock_ccxt.binance.return_value = mock_exchange

                feed = CryptoFeed(
                    exchange="binance",
                    symbols=["BTC/USDT"],
                    stream_ohlcv=True,
                )
                feed._running = True

                await feed._stream_ohlcv_for_symbol("BTC/USDT")

                assert feed._candle_count == 1

    async def test_stream_trades_error_handling(self):
        """Test error handling in trade streaming."""
        with patch("ml4t.live.feeds.crypto_feed.CCXT_AVAILABLE", True):
            with patch("ml4t.live.feeds.crypto_feed.ccxt") as mock_ccxt:
                mock_exchange = MockExchange()

                async def mock_watch_trades(symbol):
                    raise RuntimeError("Connection error")

                mock_exchange.watch_trades = mock_watch_trades
                mock_ccxt.binance.return_value = mock_exchange

                feed = CryptoFeed(
                    exchange="binance",
                    symbols=["BTC/USDT"],
                    stream_trades=True,
                    stream_ohlcv=False,
                )
                feed._running = True

                # Should not raise, just log error
                await feed._stream_trades_for_symbol("BTC/USDT")

                # No trades processed due to error
                assert feed._trade_count == 0

    async def test_stream_ohlcv_error_handling(self):
        """Test error handling in OHLCV streaming."""
        with patch("ml4t.live.feeds.crypto_feed.CCXT_AVAILABLE", True):
            with patch("ml4t.live.feeds.crypto_feed.ccxt") as mock_ccxt:
                mock_exchange = MockExchange()

                async def mock_watch_ohlcv(symbol, timeframe):
                    raise RuntimeError("Connection error")

                mock_exchange.watch_ohlcv = mock_watch_ohlcv
                mock_ccxt.binance.return_value = mock_exchange

                feed = CryptoFeed(
                    exchange="binance",
                    symbols=["BTC/USDT"],
                    stream_ohlcv=True,
                )
                feed._running = True

                # Should not raise, just log error
                await feed._stream_ohlcv_for_symbol("BTC/USDT")

                # No candles processed due to error
                assert feed._candle_count == 0

    async def test_stream_ohlcv_deduplication(self):
        """Test that duplicate candles are not emitted."""
        with patch("ml4t.live.feeds.crypto_feed.CCXT_AVAILABLE", True):
            with patch("ml4t.live.feeds.crypto_feed.ccxt") as mock_ccxt:
                mock_exchange = MockExchange()
                # No watch_ohlcv = REST fallback with deduplication logic

                # Same timestamp = duplicate
                candle_data = [[1704067200000, 50000, 51000, 49000, 50500, 100.5]]
                call_count = [0]

                async def mock_fetch_ohlcv(symbol, timeframe, limit=2):
                    call_count[0] += 1
                    if call_count[0] > 2:
                        raise asyncio.CancelledError()
                    return candle_data  # Same candle each time

                mock_exchange.fetch_ohlcv = mock_fetch_ohlcv
                mock_ccxt.binance.return_value = mock_exchange

                feed = CryptoFeed(
                    exchange="binance",
                    symbols=["BTC/USDT"],
                    stream_ohlcv=True,
                )
                feed._running = True

                # Mock sleep to avoid waiting
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    await feed._stream_ohlcv_for_symbol("BTC/USDT")

                # Only 1 candle should be processed (duplicate filtered)
                assert feed._candle_count == 1
