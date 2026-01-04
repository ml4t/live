"""Integration tests for Alpaca broker and data feed.

These tests require Alpaca paper trading credentials:
- ALPACA_API_KEY: Your Alpaca API key (starts with PK)
- ALPACA_SECRET_KEY: Your Alpaca secret key

Run with:
    pytest tests/integration/test_alpaca_integration.py -v -m integration
"""

import asyncio
import os

import pytest

from ml4t.backtest.types import OrderSide, OrderType


# Skip all tests if credentials not set
pytestmark = pytest.mark.integration


@pytest.fixture
def alpaca_credentials():
    """Get Alpaca credentials from environment."""
    api_key = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")

    if not api_key or not secret_key:
        pytest.skip("Set ALPACA_API_KEY and ALPACA_SECRET_KEY environment variables")

    return api_key, secret_key


@pytest.fixture
async def alpaca_broker(alpaca_credentials):
    """Create and connect an Alpaca broker for testing."""
    from ml4t.live import AlpacaBroker

    api_key, secret_key = alpaca_credentials

    broker = AlpacaBroker(
        api_key=api_key,
        secret_key=secret_key,
        paper=True,  # Always use paper trading for tests
    )

    await broker.connect()
    yield broker
    await broker.disconnect()


@pytest.fixture
async def alpaca_feed(alpaca_credentials):
    """Create an Alpaca data feed for testing."""
    from ml4t.live import AlpacaDataFeed

    api_key, secret_key = alpaca_credentials

    feed = AlpacaDataFeed(
        api_key=api_key,
        secret_key=secret_key,
        symbols=["AAPL"],
        data_type="bars",
    )

    await feed.start()
    yield feed
    feed.stop()


class TestAlpacaBrokerIntegration:
    """Integration tests for AlpacaBroker with real paper trading account."""

    @pytest.mark.asyncio
    async def test_connection(self, alpaca_broker):
        """Test basic connection to Alpaca."""
        assert alpaca_broker.is_connected is True

    @pytest.mark.asyncio
    async def test_account_info(self, alpaca_broker):
        """Test fetching account information."""
        account_value = await alpaca_broker.get_account_value_async()
        cash = await alpaca_broker.get_cash_async()

        assert account_value > 0, "Account value should be positive"
        assert cash >= 0, "Cash should be non-negative"
        print(f"\nAccount Value: ${account_value:,.2f}")
        print(f"Cash: ${cash:,.2f}")

    @pytest.mark.asyncio
    async def test_positions(self, alpaca_broker):
        """Test fetching positions."""
        positions = await alpaca_broker.get_positions_async()

        assert isinstance(positions, dict)
        print(f"\nPositions: {len(positions)}")
        for symbol, pos in positions.items():
            print(f"  {symbol}: {pos.quantity} shares @ ${pos.entry_price:.2f}")

    @pytest.mark.asyncio
    async def test_pending_orders(self, alpaca_broker):
        """Test fetching pending orders."""
        orders = alpaca_broker.pending_orders

        assert isinstance(orders, list)
        print(f"\nPending Orders: {len(orders)}")

    @pytest.mark.asyncio
    async def test_submit_and_cancel_order(self, alpaca_broker):
        """Test submitting and canceling a limit order."""
        # Submit a limit order at a low price (shouldn't fill)
        order = await alpaca_broker.submit_order_async(
            asset="AAPL",
            quantity=1,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            limit_price=1.00,  # Way below market, won't fill
        )

        assert order is not None
        assert order.order_id.startswith("ML4T-")
        print(f"\nSubmitted order: {order.order_id}")

        # Give Alpaca a moment to process
        await asyncio.sleep(0.5)

        # Cancel the order
        cancelled = await alpaca_broker.cancel_order_async(order.order_id)
        print(f"Cancelled: {cancelled}")

        # Note: Cancel may fail if order already filled/rejected
        # This is expected behavior in some market conditions


class TestAlpacaDataFeedIntegration:
    """Integration tests for AlpacaDataFeed with real market data."""

    @pytest.mark.asyncio
    async def test_feed_starts(self, alpaca_feed):
        """Test that the feed starts successfully."""
        assert alpaca_feed._running is True

    @pytest.mark.asyncio
    async def test_feed_stats(self, alpaca_feed):
        """Test feed statistics."""
        stats = alpaca_feed.stats

        assert stats["running"] is True
        assert stats["stock_symbols"] == ["AAPL"]
        assert stats["data_type"] == "bars"
        print(f"\nFeed Stats: {stats}")

    @pytest.mark.asyncio
    async def test_receive_bar(self, alpaca_feed):
        """Test receiving a bar from the feed.

        Note: This test may timeout outside market hours.
        Bars are only emitted during market sessions.
        """
        # Create a timeout task
        try:
            # Wait up to 120 seconds for a bar (minute bars)
            timestamp, data, context = await asyncio.wait_for(
                alpaca_feed.__anext__(),
                timeout=120.0,
            )

            assert "AAPL" in data
            print(f"\nReceived bar at {timestamp}:")
            print(f"  AAPL: {data['AAPL']}")

        except asyncio.TimeoutError:
            # Outside market hours, bars won't arrive
            pytest.skip("No bars received - market may be closed")


class TestAlpacaEndToEnd:
    """End-to-end tests combining broker and feed."""

    @pytest.mark.asyncio
    async def test_broker_and_feed_together(self, alpaca_broker, alpaca_feed):
        """Test using broker and feed simultaneously."""
        # Get account info
        account_value = await alpaca_broker.get_account_value_async()

        # Check feed is running
        assert alpaca_feed._running is True

        # Get stats
        stats = alpaca_feed.stats

        print("\n=== End-to-End Test ===")
        print(f"Account Value: ${account_value:,.2f}")
        print(f"Feed Running: {stats['running']}")
        print(f"Symbols: {stats['stock_symbols']}")

        assert account_value > 0


class TestAlpacaCryptoIntegration:
    """Integration tests for crypto trading (if enabled on account)."""

    @pytest.fixture
    async def crypto_feed(self, alpaca_credentials):
        """Create a crypto data feed for testing."""
        from ml4t.live import AlpacaDataFeed

        api_key, secret_key = alpaca_credentials

        feed = AlpacaDataFeed(
            api_key=api_key,
            secret_key=secret_key,
            symbols=["BTC/USD"],
            data_type="bars",
        )

        await feed.start()
        yield feed
        feed.stop()

    @pytest.mark.asyncio
    async def test_crypto_feed_starts(self, crypto_feed):
        """Test that crypto feed starts successfully."""
        assert crypto_feed._running is True
        stats = crypto_feed.stats

        assert stats["crypto_symbols"] == ["BTC/USD"]
        print(f"\nCrypto Feed Stats: {stats}")

    @pytest.mark.asyncio
    async def test_crypto_bar(self, crypto_feed):
        """Test receiving a crypto bar.

        Crypto trades 24/7 so this should work anytime.
        """
        try:
            timestamp, data, context = await asyncio.wait_for(
                crypto_feed.__anext__(),
                timeout=120.0,
            )

            assert "BTC/USD" in data
            print(f"\nReceived crypto bar at {timestamp}:")
            print(f"  BTC/USD: {data['BTC/USD']}")

        except asyncio.TimeoutError:
            pytest.skip("No crypto bars received - check subscription")
