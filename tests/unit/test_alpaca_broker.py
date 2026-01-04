"""Unit tests for AlpacaBroker connection and setup."""

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from alpaca.trading.enums import OrderStatus as AlpacaOrderStatus
from ml4t.backtest.types import Order, OrderSide, OrderStatus, OrderType, Position
from ml4t.live.brokers.alpaca import AlpacaBroker


class MockAlpacaAccount:
    """Mock Alpaca Account object."""

    def __init__(
        self,
        equity: str = "100000.00",
        cash: str = "50000.00",
        buying_power: str = "200000.00",
        account_number: str = "PA12345678",
    ):
        self.equity = equity
        self.cash = cash
        self.buying_power = buying_power
        self.account_number = account_number


class MockAlpacaPosition:
    """Mock Alpaca Position object."""

    def __init__(
        self,
        symbol: str,
        qty: str,
        avg_entry_price: str,
        side: str = "long",
        current_price: str | None = None,
    ):
        self.symbol = symbol
        self.qty = qty
        self.avg_entry_price = avg_entry_price
        self.side = side
        self.current_price = current_price or avg_entry_price


class MockAlpacaOrder:
    """Mock Alpaca Order object."""

    def __init__(
        self,
        id: str = "abc123-uuid",
        symbol: str = "AAPL",
        qty: str = "100",
        side: str = "buy",
        type: str = "market",
        status: str = "new",
        filled_qty: str = "0",
        filled_avg_price: str | None = None,
        limit_price: str | None = None,
        stop_price: str | None = None,
        created_at: datetime | None = None,
    ):
        self.id = id
        self.symbol = symbol
        self.qty = qty
        self.side = side
        self.type = type
        self.status = AlpacaOrderStatus.NEW if status == "new" else AlpacaOrderStatus.PENDING_NEW
        self.filled_qty = filled_qty
        self.filled_avg_price = filled_avg_price
        self.limit_price = limit_price
        self.stop_price = stop_price
        self.created_at = created_at or datetime.now(timezone.utc)


class MockTradeUpdate:
    """Mock trade update from WebSocket stream."""

    def __init__(self, event: str, order: MockAlpacaOrder):
        self.event = event
        self.order = order
        self.timestamp = datetime.now(timezone.utc)


class TestAlpacaBrokerSetup:
    """Test suite for AlpacaBroker initialization and connection."""

    def test_initialization_defaults(self):
        """Test AlpacaBroker initialization with defaults."""
        broker = AlpacaBroker(
            api_key="PKTEST123",
            secret_key="SECRETTEST",
        )

        assert broker._api_key == "PKTEST123"
        assert broker._secret_key == "SECRETTEST"
        assert broker._paper is True  # Default to paper trading
        assert broker._connected is False
        assert broker._positions == {}
        assert broker._pending_orders == {}
        assert broker._order_counter == 0
        assert broker._trading_client is None
        assert broker._trading_stream is None

    def test_initialization_live_mode(self):
        """Test AlpacaBroker initialization for live trading."""
        broker = AlpacaBroker(
            api_key="PKTEST123",
            secret_key="SECRETTEST",
            paper=False,
        )

        assert broker._paper is False

    @pytest.mark.asyncio
    @patch("ml4t.live.brokers.alpaca.TradingClient")
    @patch("ml4t.live.brokers.alpaca.TradingStream")
    async def test_connect_success(self, mock_stream_class, mock_client_class):
        """Test successful connection to Alpaca."""
        # Setup mocks
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.get_account.return_value = MockAlpacaAccount()
        mock_client.get_all_positions.return_value = []
        mock_client.get_orders.return_value = []

        mock_stream = MagicMock()
        mock_stream_class.return_value = mock_stream
        mock_stream.run = MagicMock()

        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
        await broker.connect()

        assert broker._connected is True
        mock_client_class.assert_called_once_with(
            api_key="PKTEST",
            secret_key="SECRET",
            paper=True,
        )
        mock_stream_class.assert_called_once_with(
            api_key="PKTEST",
            secret_key="SECRET",
            paper=True,
        )
        mock_stream.subscribe_trade_updates.assert_called_once()

    @pytest.mark.asyncio
    @patch("ml4t.live.brokers.alpaca.TradingClient")
    @patch("ml4t.live.brokers.alpaca.TradingStream")
    async def test_connect_syncs_positions(self, mock_stream_class, mock_client_class):
        """Test connect syncs existing positions."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.get_account.return_value = MockAlpacaAccount()
        mock_client.get_all_positions.return_value = [
            MockAlpacaPosition("AAPL", "100", "150.00", current_price="155.00"),
            MockAlpacaPosition("GOOGL", "50", "2800.00", current_price="2850.00"),
        ]
        mock_client.get_orders.return_value = []

        mock_stream = MagicMock()
        mock_stream_class.return_value = mock_stream

        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
        await broker.connect()

        assert len(broker._positions) == 2
        assert "AAPL" in broker._positions
        assert broker._positions["AAPL"].quantity == 100.0
        assert broker._positions["AAPL"].entry_price == 150.0
        assert "GOOGL" in broker._positions
        assert broker._positions["GOOGL"].quantity == 50.0

    @pytest.mark.asyncio
    @patch("ml4t.live.brokers.alpaca.TradingClient")
    @patch("ml4t.live.brokers.alpaca.TradingStream")
    async def test_connect_syncs_pending_orders(self, mock_stream_class, mock_client_class):
        """Test connect syncs existing open orders."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.get_account.return_value = MockAlpacaAccount()
        mock_client.get_all_positions.return_value = []
        mock_client.get_orders.return_value = [
            MockAlpacaOrder(id="order-1", symbol="AAPL", qty="100", side="buy", status="new"),
            MockAlpacaOrder(
                id="order-2", symbol="MSFT", qty="50", side="sell", status="pending_new"
            ),
        ]

        mock_stream = MagicMock()
        mock_stream_class.return_value = mock_stream

        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
        await broker.connect()

        assert len(broker._pending_orders) == 2
        assert "order-1" in broker._alpaca_order_map
        assert "order-2" in broker._alpaca_order_map

    @pytest.mark.asyncio
    @patch("ml4t.live.brokers.alpaca.TradingClient")
    @patch("ml4t.live.brokers.alpaca.TradingStream")
    async def test_disconnect(self, mock_stream_class, mock_client_class):
        """Test disconnect from Alpaca."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.get_account.return_value = MockAlpacaAccount()
        mock_client.get_all_positions.return_value = []
        mock_client.get_orders.return_value = []

        mock_stream = MagicMock()
        mock_stream_class.return_value = mock_stream

        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
        await broker.connect()
        await broker.disconnect()

        assert broker._connected is False
        mock_stream.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_disconnect_when_not_connected(self):
        """Test disconnect when not connected is safe."""
        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
        broker._connected = False

        # Should not raise
        await broker.disconnect()

        assert broker._connected is False

    def test_is_connected_property(self):
        """Test is_connected property."""
        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")

        assert broker.is_connected is False

        # Setting _connected=True alone is not enough, need _trading_client too
        broker._connected = True
        assert broker.is_connected is False  # Still False because _trading_client is None

        broker._trading_client = MagicMock()
        assert broker.is_connected is True


class TestAlpacaBrokerPositions:
    """Test suite for position management."""

    def test_positions_property_returns_copy(self):
        """Test positions property returns shallow copy."""
        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
        broker._positions = {
            "AAPL": Position(
                asset="AAPL", quantity=100, entry_price=150.0, entry_time=datetime.now(timezone.utc)
            ),
        }

        positions = broker.positions

        assert positions is not broker._positions
        assert "AAPL" in positions

    def test_pending_orders_property(self):
        """Test pending_orders property returns list."""
        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
        broker._pending_orders = {
            "ML4T-1": Order(
                order_id="ML4T-1",
                asset="AAPL",
                quantity=100,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                status=OrderStatus.PENDING,
            )
        }

        orders = broker.pending_orders

        assert isinstance(orders, list)
        assert len(orders) == 1
        assert orders[0].order_id == "ML4T-1"

    def test_get_position_found(self):
        """Test get_position returns position when found."""
        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
        pos = Position(
            asset="AAPL", quantity=100, entry_price=150.0, entry_time=datetime.now(timezone.utc)
        )
        broker._positions = {"AAPL": pos}

        result = broker.get_position("AAPL")
        assert result == pos

        # Case insensitive
        result = broker.get_position("aapl")
        assert result == pos

    def test_get_position_not_found(self):
        """Test get_position returns None when not found."""
        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
        broker._positions = {}

        result = broker.get_position("AAPL")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_positions_async(self):
        """Test get_positions_async returns copy."""
        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
        broker._positions = {
            "AAPL": Position(
                asset="AAPL", quantity=100, entry_price=150.0, entry_time=datetime.now(timezone.utc)
            ),
        }

        positions = await broker.get_positions_async()

        assert positions is not broker._positions
        assert "AAPL" in positions


class TestAlpacaBrokerAccount:
    """Test suite for account queries."""

    @pytest.mark.asyncio
    @patch("ml4t.live.brokers.alpaca.TradingClient")
    async def test_get_account_value_async(self, mock_client_class):
        """Test get_account_value_async returns equity."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.get_account.return_value = MockAlpacaAccount(equity="125000.50")

        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
        broker._trading_client = mock_client
        broker._connected = True

        value = await broker.get_account_value_async()

        assert value == 125000.50

    @pytest.mark.asyncio
    @patch("ml4t.live.brokers.alpaca.TradingClient")
    async def test_get_cash_async(self, mock_client_class):
        """Test get_cash_async returns available cash."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.get_account.return_value = MockAlpacaAccount(cash="75000.25")

        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
        broker._trading_client = mock_client
        broker._connected = True

        cash = await broker.get_cash_async()

        assert cash == 75000.25

    @pytest.mark.asyncio
    async def test_get_account_value_not_connected(self):
        """Test get_account_value_async returns 0 when not connected."""
        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
        broker._connected = False
        broker._trading_client = None

        value = await broker.get_account_value_async()
        assert value == 0.0

    @pytest.mark.asyncio
    async def test_get_cash_not_connected(self):
        """Test get_cash_async returns 0 when not connected."""
        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
        broker._connected = False
        broker._trading_client = None

        cash = await broker.get_cash_async()
        assert cash == 0.0


class TestAlpacaBrokerOrderSubmission:
    """Test suite for order submission."""

    @pytest.mark.asyncio
    async def test_submit_order_not_connected(self):
        """Test submit_order_async raises when not connected."""
        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
        broker._connected = False

        with pytest.raises(RuntimeError, match="Not connected to Alpaca"):
            await broker.submit_order_async("AAPL", 100)

    @pytest.mark.asyncio
    @patch("ml4t.live.brokers.alpaca.TradingClient")
    async def test_submit_market_order_buy(self, mock_client_class):
        """Test submitting a market buy order."""
        mock_client = MagicMock()
        mock_client.submit_order.return_value = MockAlpacaOrder(
            id="alpaca-order-123",
            symbol="AAPL",
            qty="100",
            side="buy",
            status="new",
        )

        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
        broker._trading_client = mock_client
        broker._connected = True

        order = await broker.submit_order_async(
            asset="AAPL",
            quantity=100,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
        )

        assert order.order_id == "ML4T-1"
        assert order.asset == "AAPL"
        assert order.side == OrderSide.BUY
        assert order.quantity == 100
        assert order.order_type == OrderType.MARKET
        assert order.status == OrderStatus.PENDING

        # Verify tracking
        assert "ML4T-1" in broker._pending_orders
        assert "alpaca-order-123" in broker._alpaca_order_map
        assert broker._alpaca_order_map["alpaca-order-123"][0] == "ML4T-1"

    @pytest.mark.asyncio
    @patch("ml4t.live.brokers.alpaca.TradingClient")
    async def test_submit_market_order_sell(self, mock_client_class):
        """Test submitting a market sell order."""
        mock_client = MagicMock()
        mock_client.submit_order.return_value = MockAlpacaOrder(
            id="alpaca-order-124",
            symbol="AAPL",
            qty="50",
            side="sell",
            status="new",
        )

        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
        broker._trading_client = mock_client
        broker._connected = True

        order = await broker.submit_order_async(
            asset="AAPL",
            quantity=-50,  # Negative for sell
        )

        assert order.side == OrderSide.SELL
        assert order.quantity == 50  # Stored as positive

    @pytest.mark.asyncio
    @patch("ml4t.live.brokers.alpaca.TradingClient")
    async def test_submit_limit_order(self, mock_client_class):
        """Test submitting a limit order."""
        mock_client = MagicMock()
        mock_client.submit_order.return_value = MockAlpacaOrder(
            id="alpaca-order-125",
            symbol="AAPL",
            qty="100",
            side="buy",
            type="limit",
            status="new",
        )

        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
        broker._trading_client = mock_client
        broker._connected = True

        order = await broker.submit_order_async(
            asset="AAPL",
            quantity=100,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            limit_price=150.00,
        )

        assert order.order_type == OrderType.LIMIT
        assert order.limit_price == 150.00

        # Verify request type
        call_args = mock_client.submit_order.call_args
        from alpaca.trading.requests import LimitOrderRequest

        assert isinstance(call_args[0][0], LimitOrderRequest)

    @pytest.mark.asyncio
    @patch("ml4t.live.brokers.alpaca.TradingClient")
    async def test_submit_stop_order(self, mock_client_class):
        """Test submitting a stop order."""
        mock_client = MagicMock()
        mock_client.submit_order.return_value = MockAlpacaOrder(
            id="alpaca-order-126",
            symbol="AAPL",
            qty="100",
            side="sell",
            type="stop",
            status="new",
        )

        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
        broker._trading_client = mock_client
        broker._connected = True

        order = await broker.submit_order_async(
            asset="AAPL",
            quantity=100,
            side=OrderSide.SELL,
            order_type=OrderType.STOP,
            stop_price=140.00,
        )

        assert order.order_type == OrderType.STOP
        assert order.stop_price == 140.00

    @pytest.mark.asyncio
    @patch("ml4t.live.brokers.alpaca.TradingClient")
    async def test_submit_stop_limit_order(self, mock_client_class):
        """Test submitting a stop-limit order."""
        mock_client = MagicMock()
        mock_client.submit_order.return_value = MockAlpacaOrder(
            id="alpaca-order-127",
            symbol="AAPL",
            qty="100",
            side="sell",
            type="stop_limit",
            status="new",
        )

        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
        broker._trading_client = mock_client
        broker._connected = True

        order = await broker.submit_order_async(
            asset="AAPL",
            quantity=100,
            side=OrderSide.SELL,
            order_type=OrderType.STOP_LIMIT,
            limit_price=139.50,
            stop_price=140.00,
        )

        assert order.order_type == OrderType.STOP_LIMIT
        assert order.limit_price == 139.50
        assert order.stop_price == 140.00

    @pytest.mark.asyncio
    @patch("ml4t.live.brokers.alpaca.TradingClient")
    async def test_order_counter_increments(self, mock_client_class):
        """Test order counter increments with each order."""
        mock_client = MagicMock()
        mock_client.submit_order.side_effect = [
            MockAlpacaOrder(id="order-1", symbol="AAPL", qty="100", side="buy"),
            MockAlpacaOrder(id="order-2", symbol="MSFT", qty="50", side="buy"),
        ]

        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
        broker._trading_client = mock_client
        broker._connected = True

        order1 = await broker.submit_order_async("AAPL", 100)
        order2 = await broker.submit_order_async("MSFT", 50)

        assert order1.order_id == "ML4T-1"
        assert order2.order_id == "ML4T-2"
        assert broker._order_counter == 2


class TestAlpacaBrokerOrderStatus:
    """Test suite for order status callbacks."""

    @pytest.mark.asyncio
    async def test_on_trade_update_fill(self):
        """Test order fill callback updates status and removes from pending."""
        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
        broker._trading_client = MagicMock()
        broker._trading_client.get_all_positions.return_value = []
        broker._connected = True

        # Create a pending order
        pending_order = Order(
            order_id="ML4T-1",
            asset="AAPL",
            quantity=100,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            status=OrderStatus.PENDING,
        )
        broker._pending_orders["ML4T-1"] = pending_order
        broker._alpaca_order_map["alpaca-123"] = ("ML4T-1", time.time())

        # Simulate fill event
        filled_order = MockAlpacaOrder(
            id="alpaca-123",
            symbol="AAPL",
            qty="100",
            side="buy",
            status="filled",
            filled_qty="100",
            filled_avg_price="150.50",
        )
        update = MockTradeUpdate(event="fill", order=filled_order)

        await broker._on_trade_update(update)

        # Order should be removed from pending_orders immediately
        assert "ML4T-1" not in broker._pending_orders
        # But alpaca_order_map cleanup is delayed (1 hour)
        assert "alpaca-123" in broker._alpaca_order_map

        # Order status should be updated
        assert pending_order.status == OrderStatus.FILLED
        assert pending_order.filled_quantity == 100
        assert pending_order.filled_price == 150.50

    @pytest.mark.asyncio
    async def test_on_trade_update_partial_fill(self):
        """Test partial fill callback updates status."""
        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
        broker._connected = True

        pending_order = Order(
            order_id="ML4T-1",
            asset="AAPL",
            quantity=100,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            status=OrderStatus.PENDING,
        )
        broker._pending_orders["ML4T-1"] = pending_order
        broker._alpaca_order_map["alpaca-123"] = ("ML4T-1", time.time())

        partial_order = MockAlpacaOrder(
            id="alpaca-123",
            symbol="AAPL",
            qty="100",
            side="buy",
            status="partially_filled",
            filled_qty="50",
            filled_avg_price="150.25",
        )
        update = MockTradeUpdate(event="partial_fill", order=partial_order)

        await broker._on_trade_update(update)

        # Order should still be in pending_orders
        assert "ML4T-1" in broker._pending_orders
        order = broker._pending_orders["ML4T-1"]
        assert order.status == OrderStatus.PENDING  # Still pending
        assert order.filled_quantity == 50

    @pytest.mark.asyncio
    async def test_on_trade_update_canceled(self):
        """Test cancel callback removes order immediately."""
        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
        broker._connected = True

        pending_order = Order(
            order_id="ML4T-1",
            asset="AAPL",
            quantity=100,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            status=OrderStatus.PENDING,
        )
        broker._pending_orders["ML4T-1"] = pending_order
        broker._alpaca_order_map["alpaca-123"] = ("ML4T-1", time.time())

        canceled_order = MockAlpacaOrder(
            id="alpaca-123",
            symbol="AAPL",
            qty="100",
            side="buy",
            status="canceled",
        )
        update = MockTradeUpdate(event="canceled", order=canceled_order)

        await broker._on_trade_update(update)

        # Order should be removed immediately
        assert "ML4T-1" not in broker._pending_orders
        assert "alpaca-123" not in broker._alpaca_order_map

    @pytest.mark.asyncio
    async def test_on_trade_update_rejected(self):
        """Test reject callback removes order immediately."""
        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
        broker._connected = True

        pending_order = Order(
            order_id="ML4T-1",
            asset="AAPL",
            quantity=100,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            status=OrderStatus.PENDING,
        )
        broker._pending_orders["ML4T-1"] = pending_order
        broker._alpaca_order_map["alpaca-123"] = ("ML4T-1", time.time())

        rejected_order = MockAlpacaOrder(
            id="alpaca-123",
            symbol="AAPL",
            qty="100",
            side="buy",
            status="rejected",
        )
        update = MockTradeUpdate(event="rejected", order=rejected_order)

        await broker._on_trade_update(update)

        # Order should be removed immediately
        assert "ML4T-1" not in broker._pending_orders
        assert "alpaca-123" not in broker._alpaca_order_map

    @pytest.mark.asyncio
    async def test_on_trade_update_unknown_order(self):
        """Test callback for unknown order logs warning but doesn't crash."""
        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
        broker._connected = True

        unknown_order = MockAlpacaOrder(id="unknown-123", symbol="AAPL", qty="100")
        update = MockTradeUpdate(event="fill", order=unknown_order)

        # Should not raise
        await broker._on_trade_update(update)

        # Nothing should be in state
        assert len(broker._pending_orders) == 0


class TestAlpacaBrokerOrderCancellation:
    """Test suite for order cancellation."""

    @pytest.mark.asyncio
    @patch("ml4t.live.brokers.alpaca.TradingClient")
    async def test_cancel_order_success(self, mock_client_class):
        """Test successful order cancellation."""
        mock_client = MagicMock()
        mock_client.cancel_order_by_id.return_value = None  # Successful cancel

        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
        broker._trading_client = mock_client
        broker._connected = True

        # Create pending order
        broker._pending_orders["ML4T-1"] = Order(
            order_id="ML4T-1",
            asset="AAPL",
            quantity=100,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            status=OrderStatus.PENDING,
        )
        broker._alpaca_order_map["alpaca-123"] = ("ML4T-1", time.time())

        result = await broker.cancel_order_async("ML4T-1")

        assert result is True
        mock_client.cancel_order_by_id.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancel_order_not_found(self):
        """Test cancel returns False when order not found."""
        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
        broker._trading_client = MagicMock()
        broker._connected = True
        broker._pending_orders = {}

        result = await broker.cancel_order_async("ML4T-999")

        assert result is False

    @pytest.mark.asyncio
    async def test_cancel_order_not_connected(self):
        """Test cancel_order_async returns False when not connected."""
        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
        broker._connected = False
        broker._trading_client = None

        result = await broker.cancel_order_async("ML4T-1")

        assert result is False


class TestAlpacaBrokerPositionClose:
    """Test suite for position closing."""

    @pytest.mark.asyncio
    async def test_close_position_no_position(self):
        """Test close_position returns None when no position exists."""
        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
        broker._trading_client = MagicMock()
        broker._connected = True
        broker._positions = {}

        result = await broker.close_position_async("AAPL")

        assert result is None

    @pytest.mark.asyncio
    @patch("ml4t.live.brokers.alpaca.TradingClient")
    async def test_close_position_long(self, mock_client_class):
        """Test closing a long position submits sell order."""
        mock_client = MagicMock()
        mock_client.submit_order.return_value = MockAlpacaOrder(
            id="alpaca-close-1",
            symbol="AAPL",
            qty="100",
            side="sell",
            status="new",
        )

        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
        broker._trading_client = mock_client
        broker._connected = True
        broker._positions = {
            "AAPL": Position(
                asset="AAPL",
                quantity=100,
                entry_price=150.0,
                entry_time=datetime.now(timezone.utc),
            )
        }

        order = await broker.close_position_async("AAPL")

        assert order is not None
        assert order.side == OrderSide.SELL
        assert order.quantity == 100

    @pytest.mark.asyncio
    @patch("ml4t.live.brokers.alpaca.TradingClient")
    async def test_close_position_short(self, mock_client_class):
        """Test closing a short position submits buy order."""
        mock_client = MagicMock()
        mock_client.submit_order.return_value = MockAlpacaOrder(
            id="alpaca-close-2",
            symbol="AAPL",
            qty="50",
            side="buy",
            status="new",
        )

        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")
        broker._trading_client = mock_client
        broker._connected = True
        broker._positions = {
            "AAPL": Position(
                asset="AAPL",
                quantity=-50,  # Short position
                entry_price=160.0,
                entry_time=datetime.now(timezone.utc),
            )
        }

        order = await broker.close_position_async("AAPL")

        assert order is not None
        assert order.side == OrderSide.BUY
        assert order.quantity == 50


class TestAlpacaBrokerStatusMapping:
    """Test suite for status mapping helper."""

    def test_map_order_status_new(self):
        """Test mapping AlpacaOrderStatus.NEW."""
        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")

        result = broker._map_order_status(AlpacaOrderStatus.NEW)

        assert result == OrderStatus.PENDING

    def test_map_order_status_filled(self):
        """Test mapping AlpacaOrderStatus.FILLED."""
        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")

        result = broker._map_order_status(AlpacaOrderStatus.FILLED)

        assert result == OrderStatus.FILLED

    def test_map_order_status_canceled(self):
        """Test mapping AlpacaOrderStatus.CANCELED."""
        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")

        result = broker._map_order_status(AlpacaOrderStatus.CANCELED)

        assert result == OrderStatus.CANCELLED

    def test_map_order_status_rejected(self):
        """Test mapping AlpacaOrderStatus.REJECTED."""
        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")

        result = broker._map_order_status(AlpacaOrderStatus.REJECTED)

        assert result == OrderStatus.REJECTED

    def test_map_order_status_expired(self):
        """Test mapping AlpacaOrderStatus.EXPIRED to cancelled."""
        broker = AlpacaBroker(api_key="PKTEST", secret_key="SECRET")

        result = broker._map_order_status(AlpacaOrderStatus.EXPIRED)

        assert result == OrderStatus.CANCELLED
