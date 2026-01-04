"""Unit tests for IBBroker connection and setup (TASK-011, TASK-013, TASK-014)."""

import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ml4t.backtest.types import Position
from ml4t.live.brokers.ib import IBBroker


class MockAccountValue:
    """Mock IB AccountValue."""

    def __init__(self, tag, value, currency="USD", account="DU12345"):
        self.tag = tag
        self.value = value
        self.currency = currency
        self.account = account


class MockPosition:
    """Mock IB Position."""

    def __init__(self, symbol, position, avgCost):
        self.contract = MagicMock()
        self.contract.symbol = symbol
        self.position = position
        self.avgCost = avgCost


class TestIBBrokerSetup:
    """Test suite for IBBroker initialization and connection."""

    def test_initialization(self):
        """Test IBBroker initialization."""
        broker = IBBroker()

        assert broker._host == "127.0.0.1"
        assert broker._port == 7497  # Paper trading default
        assert broker._client_id == 1
        assert broker._account is None
        assert not broker._connected
        assert broker._positions == {}
        assert broker._pending_orders == {}
        assert broker._order_counter == 0

    def test_custom_initialization(self):
        """Test IBBroker with custom parameters."""
        broker = IBBroker(
            host="192.168.1.100",
            port=4002,
            client_id=5,
            account="DU99999",
        )

        assert broker._host == "192.168.1.100"
        assert broker._port == 4002
        assert broker._client_id == 5
        assert broker._account == "DU99999"

    @pytest.mark.asyncio
    @patch("ml4t.live.brokers.ib.IB")
    async def test_connect_success(self, mock_ib_class):
        """Test successful connection to IB."""
        mock_ib = MagicMock()
        mock_ib_class.return_value = mock_ib
        mock_ib.managedAccounts = MagicMock(return_value=["DU12345", "DU67890"])
        mock_ib.connectAsync = AsyncMock()
        mock_ib.reqPositionsAsync = AsyncMock(return_value=[])
        mock_ib.orderStatusEvent = MagicMock()
        mock_ib.positionEvent = MagicMock()
        mock_ib.openTrades = MagicMock(return_value=[])

        broker = IBBroker()
        broker.ib = mock_ib

        await broker.connect()

        assert broker._connected
        assert broker._account == "DU12345"  # First account
        mock_ib.connectAsync.assert_called_once_with(
            host="127.0.0.1",
            port=7497,
            clientId=1,
            account=None,
            timeout=15,
        )

    @pytest.mark.asyncio
    @patch("ml4t.live.brokers.ib.IB")
    async def test_connect_with_account(self, mock_ib_class):
        """Test connection when account is pre-specified."""
        mock_ib = MagicMock()
        mock_ib_class.return_value = mock_ib
        mock_ib.managedAccounts = MagicMock(return_value=["DU12345", "DU67890"])
        mock_ib.connectAsync = AsyncMock()
        mock_ib.reqPositionsAsync = AsyncMock(return_value=[])
        mock_ib.orderStatusEvent = MagicMock()
        mock_ib.positionEvent = MagicMock()
        mock_ib.openTrades = MagicMock(return_value=[])

        broker = IBBroker(account="DU67890")
        broker.ib = mock_ib

        await broker.connect()

        assert broker._connected
        assert broker._account == "DU67890"  # Pre-specified account

    @pytest.mark.asyncio
    @patch("ml4t.live.brokers.ib.IB")
    async def test_disconnect(self, mock_ib_class):
        """Test disconnect from IB."""
        mock_ib = MagicMock()
        mock_ib_class.return_value = mock_ib

        broker = IBBroker()
        broker.ib = mock_ib
        broker._connected = True

        await broker.disconnect()

        assert not broker._connected
        mock_ib.disconnect.assert_called_once()

    @pytest.mark.asyncio
    @patch("ml4t.live.brokers.ib.IB")
    async def test_disconnect_when_not_connected(self, mock_ib_class):
        """Test disconnect when not connected (should be safe)."""
        mock_ib = MagicMock()
        mock_ib_class.return_value = mock_ib

        broker = IBBroker()
        broker.ib = mock_ib
        broker._connected = False

        await broker.disconnect()

        # Should not call disconnect
        mock_ib.disconnect.assert_not_called()

    def test_is_connected_property(self):
        """Test is_connected property."""
        broker = IBBroker()
        broker._connected = False
        broker.ib.isConnected = MagicMock(return_value=False)

        assert not broker.is_connected

        broker._connected = True
        broker.ib.isConnected = MagicMock(return_value=True)

        assert broker.is_connected

    def test_positions_property(self):
        """Test positions property returns copy."""
        broker = IBBroker()
        broker._positions = {
            "AAPL": Position(
                asset="AAPL", quantity=100, entry_price=150.0, entry_time=datetime.now()
            ),
            "GOOGL": Position(
                asset="GOOGL", quantity=50, entry_price=2800.0, entry_time=datetime.now()
            ),
        }

        positions = broker.positions

        # Should be a copy
        assert positions is not broker._positions
        assert len(positions) == 2
        assert "AAPL" in positions
        assert "GOOGL" in positions

    def test_pending_orders_property(self):
        """Test pending_orders property returns list."""
        broker = IBBroker()
        broker._pending_orders = {}

        orders = broker.pending_orders

        assert isinstance(orders, list)
        assert len(orders) == 0

    def test_get_position(self):
        """Test get_position method."""
        broker = IBBroker()
        pos = Position(asset="AAPL", quantity=100, entry_price=150.0, entry_time=datetime.now())
        broker._positions = {"AAPL": pos}

        result = broker.get_position("AAPL")
        assert result == pos

        result = broker.get_position("aapl")  # Should work with lowercase
        assert result == pos

        result = broker.get_position("GOOGL")
        assert result is None

    @pytest.mark.asyncio
    @patch("ml4t.live.brokers.ib.IB")
    async def test_get_positions_async(self, mock_ib_class):
        """Test async get_positions_async method."""
        broker = IBBroker()
        pos = Position(asset="AAPL", quantity=100, entry_price=150.0, entry_time=datetime.now())
        broker._positions = {"AAPL": pos}

        positions = await broker.get_positions_async()

        assert positions is not broker._positions  # Should be a copy
        assert "AAPL" in positions

    @pytest.mark.asyncio
    @patch("ml4t.live.brokers.ib.IB")
    async def test_get_account_value_async(self, mock_ib_class):
        """Test get_account_value_async method."""
        mock_ib = MagicMock()
        mock_ib_class.return_value = mock_ib
        mock_ib.accountValues.return_value = [
            MockAccountValue("NetLiquidation", "125000.50", "USD", "DU12345"),
            MockAccountValue("AvailableFunds", "75000.25", "USD", "DU12345"),
        ]

        broker = IBBroker(account="DU12345")
        broker.ib = mock_ib

        value = await broker.get_account_value_async()

        assert value == 125000.50

    @pytest.mark.asyncio
    @patch("ml4t.live.brokers.ib.IB")
    async def test_get_cash_async(self, mock_ib_class):
        """Test get_cash_async method."""
        mock_ib = MagicMock()
        mock_ib_class.return_value = mock_ib
        mock_ib.accountValues.return_value = [
            MockAccountValue("NetLiquidation", "125000.50", "USD", "DU12345"),
            MockAccountValue("AvailableFunds", "75000.25", "USD", "DU12345"),
        ]

        broker = IBBroker(account="DU12345")
        broker.ib = mock_ib

        cash = await broker.get_cash_async()

        assert cash == 75000.25

    @pytest.mark.asyncio
    async def test_submit_order_async_not_connected(self):
        """Test submit_order_async raises when not connected."""
        broker = IBBroker()
        broker._connected = False

        with pytest.raises(RuntimeError, match="Not connected to IB"):
            await broker.submit_order_async("AAPL", 100)

    @pytest.mark.asyncio
    async def test_close_position_async_no_position(self):
        """Test close_position_async returns None when no position."""
        broker = IBBroker()
        broker._positions = {}

        result = await broker.close_position_async("AAPL")

        assert result is None

    def test_get_contract(self):
        """Test _get_contract method."""
        broker = IBBroker()

        contract = broker._get_contract("AAPL")

        assert contract.symbol == "AAPL"
        assert contract.exchange == "SMART"
        assert contract.currency == "USD"

        # Should cache contract
        contract2 = broker._get_contract("AAPL")
        assert contract2 is contract

    def test_on_position_update(self):
        """Test _on_position method."""
        broker = IBBroker()

        mock_pos = MockPosition("AAPL", 100, 150.0)
        broker._on_position(mock_pos)

        assert "AAPL" in broker._positions
        pos = broker._positions["AAPL"]
        assert pos.asset == "AAPL"
        assert pos.quantity == 100.0
        assert pos.entry_price == 150.0

    def test_on_position_delete(self):
        """Test _on_position deletes position when quantity is 0."""
        broker = IBBroker()
        broker._positions = {
            "AAPL": Position(
                asset="AAPL", quantity=100, entry_price=150.0, entry_time=datetime.now()
            )
        }

        mock_pos = MockPosition("AAPL", 0, 0.0)
        broker._on_position(mock_pos)

        assert "AAPL" not in broker._positions

    @pytest.mark.asyncio
    @patch("ml4t.live.brokers.ib.IB")
    async def test_sync_positions(self, mock_ib_class):
        """Test _sync_positions method."""
        mock_ib = MagicMock()
        mock_ib_class.return_value = mock_ib

        mock_positions = [
            MockPosition("AAPL", 100, 150.0),
            MockPosition("GOOGL", 50, 2800.0),
        ]
        mock_ib.reqPositionsAsync = AsyncMock(return_value=mock_positions)

        broker = IBBroker()
        broker.ib = mock_ib

        await broker._sync_positions()

        assert len(broker._positions) == 2
        assert "AAPL" in broker._positions
        assert "GOOGL" in broker._positions

    @pytest.mark.asyncio
    @patch("ml4t.live.brokers.ib.IB")
    async def test_sync_orders_empty(self, mock_ib_class):
        """Test _sync_orders with no open trades."""
        mock_ib = MagicMock()
        mock_ib.openTrades = MagicMock(return_value=[])

        broker = IBBroker()
        broker.ib = mock_ib

        await broker._sync_orders()

        assert len(broker._pending_orders) == 0
        assert len(broker._ib_order_map) == 0


class TestOrderSubmission:
    """Test suite for order submission (TASK-013)."""

    @pytest.mark.asyncio
    @patch("ml4t.live.brokers.ib.IB")
    async def test_submit_market_order(self, mock_ib_class):
        """Test submitting a market order."""
        from ml4t.backtest.types import OrderSide, OrderType, OrderStatus

        mock_ib = MagicMock()
        mock_ib.isConnected = MagicMock(return_value=True)

        # Mock trade object
        mock_trade = MagicMock()
        mock_trade.order.orderId = 123
        mock_ib.placeOrder = MagicMock(return_value=mock_trade)

        broker = IBBroker()
        broker.ib = mock_ib
        broker._connected = True

        order = await broker.submit_order_async(
            asset="AAPL", quantity=100, side=OrderSide.BUY, order_type=OrderType.MARKET
        )

        # Verify order was created
        assert order.asset == "AAPL"
        assert order.side == OrderSide.BUY
        assert order.quantity == 100
        assert order.order_type == OrderType.MARKET
        assert order.status == OrderStatus.PENDING
        assert order.order_id == "ML4T-1"

        # Verify order was tracked
        assert len(broker._pending_orders) == 1
        assert "ML4T-1" in broker._pending_orders
        assert 123 in broker._ib_order_map
        assert broker._ib_order_map[123][0] == "ML4T-1"

        # Verify IB placeOrder was called
        mock_ib.placeOrder.assert_called_once()

    @pytest.mark.asyncio
    @patch("ml4t.live.brokers.ib.IB")
    async def test_submit_limit_order(self, mock_ib_class):
        """Test submitting a limit order."""
        from ml4t.backtest.types import OrderSide, OrderType

        mock_ib = MagicMock()
        mock_ib.isConnected = MagicMock(return_value=True)

        mock_trade = MagicMock()
        mock_trade.order.orderId = 124
        mock_ib.placeOrder = MagicMock(return_value=mock_trade)

        broker = IBBroker()
        broker.ib = mock_ib
        broker._connected = True

        order = await broker.submit_order_async(
            asset="GOOGL",
            quantity=50,
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            limit_price=150.0,
        )

        assert order.asset == "GOOGL"
        assert order.side == OrderSide.SELL
        assert order.quantity == 50
        assert order.order_type == OrderType.LIMIT
        assert order.limit_price == 150.0

    @pytest.mark.asyncio
    @patch("ml4t.live.brokers.ib.IB")
    async def test_submit_stop_order(self, mock_ib_class):
        """Test submitting a stop order."""
        from ml4t.backtest.types import OrderSide, OrderType

        mock_ib = MagicMock()
        mock_ib.isConnected = MagicMock(return_value=True)

        mock_trade = MagicMock()
        mock_trade.order.orderId = 125
        mock_ib.placeOrder = MagicMock(return_value=mock_trade)

        broker = IBBroker()
        broker.ib = mock_ib
        broker._connected = True

        order = await broker.submit_order_async(
            asset="TSLA",
            quantity=25,
            side=OrderSide.BUY,
            order_type=OrderType.STOP,
            stop_price=200.0,
        )

        assert order.asset == "TSLA"
        assert order.order_type == OrderType.STOP
        assert order.stop_price == 200.0

    @pytest.mark.asyncio
    @patch("ml4t.live.brokers.ib.IB")
    async def test_submit_stop_limit_order(self, mock_ib_class):
        """Test submitting a stop-limit order."""
        from ml4t.backtest.types import OrderSide, OrderType

        mock_ib = MagicMock()
        mock_ib.isConnected = MagicMock(return_value=True)

        mock_trade = MagicMock()
        mock_trade.order.orderId = 126
        mock_ib.placeOrder = MagicMock(return_value=mock_trade)

        broker = IBBroker()
        broker.ib = mock_ib
        broker._connected = True

        order = await broker.submit_order_async(
            asset="MSFT",
            quantity=75,
            side=OrderSide.SELL,
            order_type=OrderType.STOP_LIMIT,
            limit_price=300.0,
            stop_price=305.0,
        )

        assert order.asset == "MSFT"
        assert order.order_type == OrderType.STOP_LIMIT
        assert order.limit_price == 300.0
        assert order.stop_price == 305.0

    @pytest.mark.asyncio
    @patch("ml4t.live.brokers.ib.IB")
    async def test_submit_order_not_connected(self, mock_ib_class):
        """Test submitting order when not connected raises error."""
        from ml4t.backtest.types import OrderSide

        broker = IBBroker()
        broker._connected = False

        with pytest.raises(RuntimeError, match="Not connected"):
            await broker.submit_order_async(asset="AAPL", quantity=100, side=OrderSide.BUY)

    @pytest.mark.asyncio
    @patch("ml4t.live.brokers.ib.IB")
    async def test_submit_order_auto_side_detection_long(self, mock_ib_class):
        """Test auto-detecting side for long position."""
        from ml4t.backtest.types import OrderSide, Position

        mock_ib = MagicMock()
        mock_ib.isConnected = MagicMock(return_value=True)

        mock_trade = MagicMock()
        mock_trade.order.orderId = 127
        mock_ib.placeOrder = MagicMock(return_value=mock_trade)

        broker = IBBroker()
        broker.ib = mock_ib
        broker._connected = True

        # Set up long position
        broker._positions["AAPL"] = Position(
            asset="AAPL", quantity=100, entry_price=150.0, entry_time=datetime.now()
        )

        # Don't specify side - should auto-detect as BUY
        order = await broker.submit_order_async(asset="AAPL", quantity=50)

        assert order.side == OrderSide.BUY

    @pytest.mark.asyncio
    @patch("ml4t.live.brokers.ib.IB")
    async def test_submit_order_auto_side_detection_short(self, mock_ib_class):
        """Test auto-detecting side for short position."""
        from ml4t.backtest.types import OrderSide, Position

        mock_ib = MagicMock()
        mock_ib.isConnected = MagicMock(return_value=True)

        mock_trade = MagicMock()
        mock_trade.order.orderId = 128
        mock_ib.placeOrder = MagicMock(return_value=mock_trade)

        broker = IBBroker()
        broker.ib = mock_ib
        broker._connected = True

        # Set up short position
        broker._positions["TSLA"] = Position(
            asset="TSLA", quantity=-100, entry_price=200.0, entry_time=datetime.now()
        )

        # Don't specify side - should auto-detect as BUY (to close short)
        order = await broker.submit_order_async(asset="TSLA", quantity=50)

        assert order.side == OrderSide.BUY

    @pytest.mark.asyncio
    @patch("ml4t.live.brokers.ib.IB")
    async def test_submit_multiple_orders(self, mock_ib_class):
        """Test submitting multiple orders maintains proper counter."""
        from ml4t.backtest.types import OrderSide

        mock_ib = MagicMock()
        mock_ib.isConnected = MagicMock(return_value=True)

        # Mock different trade objects for each order
        mock_trade1 = MagicMock()
        mock_trade1.order.orderId = 100
        mock_trade2 = MagicMock()
        mock_trade2.order.orderId = 101
        mock_trade3 = MagicMock()
        mock_trade3.order.orderId = 102

        mock_ib.placeOrder = MagicMock(side_effect=[mock_trade1, mock_trade2, mock_trade3])

        broker = IBBroker()
        broker.ib = mock_ib
        broker._connected = True

        order1 = await broker.submit_order_async("AAPL", 100, OrderSide.BUY)
        order2 = await broker.submit_order_async("GOOGL", 50, OrderSide.SELL)
        order3 = await broker.submit_order_async("MSFT", 75, OrderSide.BUY)

        assert order1.order_id == "ML4T-1"
        assert order2.order_id == "ML4T-2"
        assert order3.order_id == "ML4T-3"
        assert len(broker._pending_orders) == 3
        assert len(broker._ib_order_map) == 3

    @pytest.mark.asyncio
    @patch("ml4t.live.brokers.ib.IB")
    async def test_sync_orders_with_open_trades(self, mock_ib_class):
        """Test _sync_orders with existing open orders (TASK-013)."""
        mock_ib = MagicMock()

        # Create mock trade objects
        mock_trade1 = MagicMock()
        mock_trade1.order.action = "BUY"
        mock_trade1.order.totalQuantity = 100
        mock_trade1.order.orderId = 200
        mock_trade1.orderStatus.status = "Submitted"
        mock_trade1.contract.symbol = "AAPL"

        mock_trade2 = MagicMock()
        mock_trade2.order.action = "SELL"
        mock_trade2.order.totalQuantity = 50
        mock_trade2.order.orderId = 201
        mock_trade2.orderStatus.status = "PreSubmitted"
        mock_trade2.contract.symbol = "GOOGL"

        mock_ib.openTrades = MagicMock(return_value=[mock_trade1, mock_trade2])

        broker = IBBroker()
        broker.ib = mock_ib

        await broker._sync_orders()

        # Should have synced both orders
        assert len(broker._pending_orders) == 2
        assert len(broker._ib_order_map) == 2

        # Check first order
        order1 = broker._pending_orders["ML4T-1"]
        assert order1.asset == "AAPL"
        assert order1.quantity == 100

        # Check second order
        order2 = broker._pending_orders["ML4T-2"]
        assert order2.asset == "GOOGL"
        assert order2.quantity == 50

    @pytest.mark.asyncio
    @patch("ml4t.live.brokers.ib.IB")
    async def test_order_counter_persistence(self, mock_ib_class):
        """Test order counter continues across submit and sync."""
        from ml4t.backtest.types import OrderSide

        mock_ib = MagicMock()
        mock_ib.isConnected = MagicMock(return_value=True)

        # First, sync existing orders
        mock_trade_sync = MagicMock()
        mock_trade_sync.order.action = "BUY"
        mock_trade_sync.order.totalQuantity = 100
        mock_trade_sync.order.orderId = 300
        mock_trade_sync.orderStatus.status = "Submitted"
        mock_trade_sync.contract.symbol = "AAPL"

        mock_ib.openTrades = MagicMock(return_value=[mock_trade_sync])

        broker = IBBroker()
        broker.ib = mock_ib
        broker._connected = True

        await broker._sync_orders()

        # Should be ML4T-1
        assert "ML4T-1" in broker._pending_orders

        # Now submit a new order
        mock_trade_new = MagicMock()
        mock_trade_new.order.orderId = 301
        mock_ib.placeOrder = MagicMock(return_value=mock_trade_new)

        order = await broker.submit_order_async("GOOGL", 50, OrderSide.SELL)

        # Should be ML4T-2 (counter continues)
        assert order.order_id == "ML4T-2"
        assert len(broker._pending_orders) == 2

    @pytest.mark.asyncio
    @patch("ml4t.live.brokers.ib.IB")
    async def test_asset_uppercase_normalization(self, mock_ib_class):
        """Test asset symbols are normalized to uppercase."""
        from ml4t.backtest.types import OrderSide

        mock_ib = MagicMock()
        mock_ib.isConnected = MagicMock(return_value=True)

        mock_trade = MagicMock()
        mock_trade.order.orderId = 129
        mock_ib.placeOrder = MagicMock(return_value=mock_trade)

        broker = IBBroker()
        broker.ib = mock_ib
        broker._connected = True

        order = await broker.submit_order_async(
            asset="aapl",  # lowercase
            quantity=100,
            side=OrderSide.BUY,
        )

        # Should be uppercase in order
        assert order.asset == "AAPL"


class TestOrderStatusCallbacks:
    """Test suite for order status callbacks (TASK-014)."""

    def test_on_order_status_filled(self):
        """Test _on_order_status handles filled orders."""
        from ml4t.backtest.types import OrderSide, OrderType, OrderStatus, Order

        broker = IBBroker()

        # Create a pending order
        order = Order(
            asset="AAPL",
            side=OrderSide.BUY,
            quantity=100,
            order_type=OrderType.MARKET,
            order_id="ML4T-1",
            status=OrderStatus.PENDING,
            created_at=datetime.now(),
        )
        broker._pending_orders["ML4T-1"] = order
        broker._ib_order_map[123] = ("ML4T-1", time.time())

        # Create mock trade with filled status
        mock_trade = MagicMock()
        mock_trade.order.orderId = 123
        mock_trade.orderStatus.status = "Filled"
        mock_trade.orderStatus.avgFillPrice = 150.5
        mock_trade.orderStatus.filled = 100

        # Call the callback
        broker._on_order_status(mock_trade)

        # Verify order was updated and removed from pending
        assert order.status == OrderStatus.FILLED
        assert order.filled_price == 150.5
        assert order.filled_quantity == 100
        assert order.filled_at is not None
        assert "ML4T-1" not in broker._pending_orders
        # _ib_order_map cleanup is scheduled (not immediate for filled orders)
        assert 123 in broker._ib_order_map

    def test_on_order_status_cancelled(self):
        """Test _on_order_status handles cancelled orders."""
        from ml4t.backtest.types import OrderSide, OrderType, OrderStatus, Order

        broker = IBBroker()

        # Create a pending order
        order = Order(
            asset="GOOGL",
            side=OrderSide.SELL,
            quantity=50,
            order_type=OrderType.LIMIT,
            limit_price=2800.0,
            order_id="ML4T-2",
            status=OrderStatus.PENDING,
            created_at=datetime.now(),
        )
        broker._pending_orders["ML4T-2"] = order
        broker._ib_order_map[124] = ("ML4T-2", time.time())

        # Create mock trade with cancelled status
        mock_trade = MagicMock()
        mock_trade.order.orderId = 124
        mock_trade.orderStatus.status = "Cancelled"

        # Call the callback
        broker._on_order_status(mock_trade)

        # Verify order was updated, removed from pending, and cleanup was immediate
        assert order.status == OrderStatus.CANCELLED
        assert "ML4T-2" not in broker._pending_orders
        assert 124 not in broker._ib_order_map  # Immediate cleanup for cancelled

    def test_on_order_status_unknown_order(self):
        """Test _on_order_status ignores unknown IB order IDs."""
        broker = IBBroker()

        # Create mock trade for unknown order
        mock_trade = MagicMock()
        mock_trade.order.orderId = 999
        mock_trade.orderStatus.status = "Filled"

        # Should not raise, just return early
        broker._on_order_status(mock_trade)

        # Nothing should change
        assert len(broker._pending_orders) == 0
        assert len(broker._ib_order_map) == 0

    def test_on_order_status_already_processed(self):
        """Test _on_order_status handles already processed orders gracefully."""
        broker = IBBroker()

        # Map exists but order already removed
        broker._ib_order_map[125] = ("ML4T-3", time.time())

        # Create mock trade
        mock_trade = MagicMock()
        mock_trade.order.orderId = 125
        mock_trade.orderStatus.status = "Filled"

        # Should not raise, just return early
        broker._on_order_status(mock_trade)

        # Map still exists (no cleanup triggered)
        assert 125 in broker._ib_order_map

    def test_on_order_status_partial_fill(self):
        """Test _on_order_status with partial fill (doesn't remove from pending)."""
        from ml4t.backtest.types import OrderSide, OrderType, OrderStatus, Order

        broker = IBBroker()

        # Create a pending order
        order = Order(
            asset="TSLA",
            side=OrderSide.BUY,
            quantity=100,
            order_type=OrderType.MARKET,
            order_id="ML4T-4",
            status=OrderStatus.PENDING,
            created_at=datetime.now(),
        )
        broker._pending_orders["ML4T-4"] = order
        broker._ib_order_map[126] = ("ML4T-4", time.time())

        # Create mock trade with 'PartiallyFilled' status (not 'Filled')
        mock_trade = MagicMock()
        mock_trade.order.orderId = 126
        mock_trade.orderStatus.status = "PartiallyFilled"
        mock_trade.orderStatus.avgFillPrice = 200.0
        mock_trade.orderStatus.filled = 50

        # Call the callback
        broker._on_order_status(mock_trade)

        # Order should remain pending (only 'Filled' and 'Cancelled' handled)
        assert order.status == OrderStatus.PENDING
        assert "ML4T-4" in broker._pending_orders
        assert 126 in broker._ib_order_map

    def test_on_order_status_multiple_orders(self):
        """Test _on_order_status handles multiple orders correctly."""
        from ml4t.backtest.types import OrderSide, OrderType, OrderStatus, Order

        broker = IBBroker()

        # Create multiple pending orders
        order1 = Order(
            asset="AAPL",
            side=OrderSide.BUY,
            quantity=100,
            order_type=OrderType.MARKET,
            order_id="ML4T-10",
            status=OrderStatus.PENDING,
            created_at=datetime.now(),
        )
        order2 = Order(
            asset="GOOGL",
            side=OrderSide.SELL,
            quantity=50,
            order_type=OrderType.LIMIT,
            limit_price=2800.0,
            order_id="ML4T-11",
            status=OrderStatus.PENDING,
            created_at=datetime.now(),
        )

        broker._pending_orders["ML4T-10"] = order1
        broker._pending_orders["ML4T-11"] = order2
        broker._ib_order_map[200] = ("ML4T-10", time.time())
        broker._ib_order_map[201] = ("ML4T-11", time.time())

        # Fill first order
        mock_trade1 = MagicMock()
        mock_trade1.order.orderId = 200
        mock_trade1.orderStatus.status = "Filled"
        mock_trade1.orderStatus.avgFillPrice = 150.0
        mock_trade1.orderStatus.filled = 100
        broker._on_order_status(mock_trade1)

        # Cancel second order
        mock_trade2 = MagicMock()
        mock_trade2.order.orderId = 201
        mock_trade2.orderStatus.status = "Cancelled"
        broker._on_order_status(mock_trade2)

        # Verify both processed correctly
        assert order1.status == OrderStatus.FILLED
        assert order2.status == OrderStatus.CANCELLED
        assert len(broker._pending_orders) == 0
        assert 200 in broker._ib_order_map  # Delayed cleanup
        assert 201 not in broker._ib_order_map  # Immediate cleanup

    @pytest.mark.asyncio
    async def test_order_lifecycle_integration(self):
        """Test full order lifecycle: submit → callback → filled."""
        from ml4t.backtest.types import OrderSide, OrderStatus

        mock_ib = MagicMock()
        mock_ib.isConnected = MagicMock(return_value=True)

        # Mock trade returned by placeOrder
        mock_trade = MagicMock()
        mock_trade.order.orderId = 300
        mock_ib.placeOrder = MagicMock(return_value=mock_trade)

        broker = IBBroker()
        broker.ib = mock_ib
        broker._connected = True

        # Submit order
        order = await broker.submit_order_async(asset="AAPL", quantity=100, side=OrderSide.BUY)

        # Verify order is pending
        assert order.status == OrderStatus.PENDING
        assert len(broker._pending_orders) == 1
        assert 300 in broker._ib_order_map

        # Simulate IB callback for filled order
        mock_trade_callback = MagicMock()
        mock_trade_callback.order.orderId = 300
        mock_trade_callback.orderStatus.status = "Filled"
        mock_trade_callback.orderStatus.avgFillPrice = 150.25
        mock_trade_callback.orderStatus.filled = 100

        broker._on_order_status(mock_trade_callback)

        # Verify order was filled
        assert order.status == OrderStatus.FILLED
        assert order.filled_price == 150.25
        assert order.filled_quantity == 100
        assert len(broker._pending_orders) == 0

    def test_memory_leak_prevention_filled_orders(self):
        """Test that filled orders schedule cleanup (memory leak prevention)."""
        from ml4t.backtest.types import OrderSide, OrderType, OrderStatus, Order

        broker = IBBroker()

        # Create order
        order = Order(
            asset="MSFT",
            side=OrderSide.BUY,
            quantity=75,
            order_type=OrderType.MARKET,
            order_id="ML4T-5",
            status=OrderStatus.PENDING,
            created_at=datetime.now(),
        )
        broker._pending_orders["ML4T-5"] = order
        broker._ib_order_map[127] = ("ML4T-5", time.time())

        # Mock asyncio.get_event_loop().call_later
        with patch("asyncio.get_event_loop") as mock_get_loop:
            mock_loop = MagicMock()
            mock_get_loop.return_value = mock_loop

            # Create filled trade
            mock_trade = MagicMock()
            mock_trade.order.orderId = 127
            mock_trade.orderStatus.status = "Filled"
            mock_trade.orderStatus.avgFillPrice = 300.0
            mock_trade.orderStatus.filled = 75

            broker._on_order_status(mock_trade)

            # Verify call_later was called with 3600 seconds (1 hour)
            mock_loop.call_later.assert_called_once()
            args = mock_loop.call_later.call_args
            assert args[0][0] == 3600  # 1 hour delay

    def test_memory_leak_prevention_cancelled_orders(self):
        """Test that cancelled orders cleanup immediately (no memory leak)."""
        from ml4t.backtest.types import OrderSide, OrderType, OrderStatus, Order

        broker = IBBroker()

        # Create order
        order = Order(
            asset="AMZN",
            side=OrderSide.SELL,
            quantity=20,
            order_type=OrderType.LIMIT,
            limit_price=3500.0,
            order_id="ML4T-6",
            status=OrderStatus.PENDING,
            created_at=datetime.now(),
        )
        broker._pending_orders["ML4T-6"] = order
        broker._ib_order_map[128] = ("ML4T-6", time.time())

        # Create cancelled trade
        mock_trade = MagicMock()
        mock_trade.order.orderId = 128
        mock_trade.orderStatus.status = "Cancelled"

        broker._on_order_status(mock_trade)

        # Verify immediate cleanup (not scheduled)
        assert order.status == OrderStatus.CANCELLED
        assert 128 not in broker._ib_order_map  # Immediate removal
        assert "ML4T-6" not in broker._pending_orders


class TestOrderCancellation:
    """Test suite for order cancellation (TASK-016)."""

    @pytest.mark.asyncio
    async def test_cancel_order_success(self):
        """Test successful order cancellation."""
        from ml4t.backtest.types import OrderSide, OrderType, OrderStatus, Order

        broker = IBBroker()

        # Create pending order
        order = Order(
            asset="AAPL",
            side=OrderSide.BUY,
            quantity=100,
            order_type=OrderType.MARKET,
            order_id="ML4T-1",
            status=OrderStatus.PENDING,
            created_at=datetime.now(),
        )
        broker._pending_orders["ML4T-1"] = order
        broker._ib_order_map[123] = ("ML4T-1", time.time())

        # Mock open trade
        mock_trade = MagicMock()
        mock_trade.order.orderId = 123
        mock_ib = MagicMock()
        mock_ib.openTrades = MagicMock(return_value=[mock_trade])
        mock_ib.cancelOrder = MagicMock()
        broker.ib = mock_ib

        # Cancel order
        result = await broker.cancel_order_async("ML4T-1")

        # Verify cancellation requested
        assert result is True
        mock_ib.cancelOrder.assert_called_once_with(mock_trade.order)

    @pytest.mark.asyncio
    async def test_cancel_order_not_found_in_map(self):
        """Test cancelling order not in tracking map returns False."""
        broker = IBBroker()
        broker.ib = MagicMock()

        # Try to cancel non-existent order
        result = await broker.cancel_order_async("ML4T-999")

        # Should return False
        assert result is False

    @pytest.mark.asyncio
    async def test_cancel_order_not_in_open_trades(self):
        """Test cancelling order not in open trades (already filled/cancelled)."""
        broker = IBBroker()

        # Order in map but not in open trades
        broker._ib_order_map[124] = ("ML4T-2", time.time())

        # Mock empty open trades
        mock_ib = MagicMock()
        mock_ib.openTrades = MagicMock(return_value=[])
        broker.ib = mock_ib

        # Try to cancel
        result = await broker.cancel_order_async("ML4T-2")

        # Should return False (order not found in open trades)
        assert result is False

    @pytest.mark.asyncio
    async def test_cancel_order_already_filled(self):
        """Test cancelling order that was already filled."""

        broker = IBBroker()

        # Order ML4T-3 was filled (still in tracking map but not in open trades)
        broker._ib_order_map[125] = ("ML4T-3", time.time())

        # Not in open trades (filled orders are removed)
        mock_ib = MagicMock()
        mock_ib.openTrades = MagicMock(return_value=[])
        broker.ib = mock_ib

        # Try to cancel
        result = await broker.cancel_order_async("ML4T-3")

        # Should return False
        assert result is False

    @pytest.mark.asyncio
    async def test_cancel_multiple_orders(self):
        """Test cancelling specific order when multiple orders exist."""
        from ml4t.backtest.types import OrderSide, OrderType, OrderStatus, Order

        broker = IBBroker()

        # Create two pending orders
        order1 = Order(
            asset="AAPL",
            side=OrderSide.BUY,
            quantity=100,
            order_type=OrderType.MARKET,
            order_id="ML4T-10",
            status=OrderStatus.PENDING,
            created_at=datetime.now(),
        )
        order2 = Order(
            asset="GOOGL",
            side=OrderSide.SELL,
            quantity=50,
            order_type=OrderType.LIMIT,
            limit_price=2800.0,
            order_id="ML4T-11",
            status=OrderStatus.PENDING,
            created_at=datetime.now(),
        )

        broker._pending_orders["ML4T-10"] = order1
        broker._pending_orders["ML4T-11"] = order2
        broker._ib_order_map[200] = ("ML4T-10", time.time())
        broker._ib_order_map[201] = ("ML4T-11", time.time())

        # Mock two open trades
        mock_trade1 = MagicMock()
        mock_trade1.order.orderId = 200
        mock_trade2 = MagicMock()
        mock_trade2.order.orderId = 201

        mock_ib = MagicMock()
        mock_ib.openTrades = MagicMock(return_value=[mock_trade1, mock_trade2])
        mock_ib.cancelOrder = MagicMock()
        broker.ib = mock_ib

        # Cancel only the second order
        result = await broker.cancel_order_async("ML4T-11")

        # Verify only second order cancelled
        assert result is True
        mock_ib.cancelOrder.assert_called_once_with(mock_trade2.order)

    @pytest.mark.asyncio
    async def test_cancel_order_lifecycle_integration(self):
        """Test full lifecycle: submit → cancel → callback."""
        from ml4t.backtest.types import OrderSide, OrderStatus

        mock_ib = MagicMock()
        mock_ib.isConnected = MagicMock(return_value=True)

        # Mock trade for submission
        mock_trade_submit = MagicMock()
        mock_trade_submit.order.orderId = 300
        mock_ib.placeOrder = MagicMock(return_value=mock_trade_submit)

        broker = IBBroker()
        broker.ib = mock_ib
        broker._connected = True

        # Submit order
        order = await broker.submit_order_async(asset="TSLA", quantity=25, side=OrderSide.BUY)

        # Verify order is pending
        assert order.status == OrderStatus.PENDING
        assert len(broker._pending_orders) == 1

        # Mock open trades for cancellation
        mock_trade_cancel = MagicMock()
        mock_trade_cancel.order.orderId = 300
        mock_ib.openTrades = MagicMock(return_value=[mock_trade_cancel])
        mock_ib.cancelOrder = MagicMock()

        # Cancel order
        result = await broker.cancel_order_async(order.order_id)
        assert result is True

        # Simulate IB callback for cancelled order
        mock_trade_callback = MagicMock()
        mock_trade_callback.order.orderId = 300
        mock_trade_callback.orderStatus.status = "Cancelled"

        broker._on_order_status(mock_trade_callback)

        # Verify order was cancelled
        assert order.status == OrderStatus.CANCELLED
        assert len(broker._pending_orders) == 0
        assert 300 not in broker._ib_order_map  # Immediate cleanup

    @pytest.mark.asyncio
    async def test_cancel_order_with_wrong_ib_order_id(self):
        """Test that cancel matches correct IB order ID."""
        broker = IBBroker()

        # Multiple orders in tracking map
        broker._ib_order_map[100] = ("ML4T-1", time.time())
        broker._ib_order_map[101] = ("ML4T-2", time.time())
        broker._ib_order_map[102] = ("ML4T-3", time.time())

        # Mock open trades
        mock_trade1 = MagicMock()
        mock_trade1.order.orderId = 100
        mock_trade2 = MagicMock()
        mock_trade2.order.orderId = 101
        mock_trade3 = MagicMock()
        mock_trade3.order.orderId = 102

        mock_ib = MagicMock()
        mock_ib.openTrades = MagicMock(return_value=[mock_trade1, mock_trade2, mock_trade3])
        mock_ib.cancelOrder = MagicMock()
        broker.ib = mock_ib

        # Cancel ML4T-2 specifically
        result = await broker.cancel_order_async("ML4T-2")

        # Verify correct order cancelled (IB order ID 101)
        assert result is True
        mock_ib.cancelOrder.assert_called_once()
        cancelled_order = mock_ib.cancelOrder.call_args[0][0]
        assert cancelled_order.orderId == 101

    @pytest.mark.asyncio
    async def test_cancel_order_returns_false_gracefully(self):
        """Test cancel_order returns False without raising on edge cases."""
        broker = IBBroker()
        mock_ib = MagicMock()
        mock_ib.openTrades = MagicMock(return_value=[])
        broker.ib = mock_ib

        # Should not raise, just return False
        result1 = await broker.cancel_order_async("NONEXISTENT")
        assert result1 is False

        # Add to map but not in open trades
        broker._ib_order_map[999] = ("ML4T-999", time.time())
        result2 = await broker.cancel_order_async("ML4T-999")
        assert result2 is False

    @pytest.mark.asyncio
    async def test_close_position_uses_cancel(self):
        """Test that close_position_async uses cancel_order if order submitted."""
        from ml4t.backtest.types import OrderSide, Position

        mock_ib = MagicMock()
        mock_ib.isConnected = MagicMock(return_value=True)

        # Mock trade for submission
        mock_trade = MagicMock()
        mock_trade.order.orderId = 400
        mock_ib.placeOrder = MagicMock(return_value=mock_trade)

        broker = IBBroker()
        broker.ib = mock_ib
        broker._connected = True

        # Create a long position
        broker._positions["MSFT"] = Position(
            asset="MSFT", quantity=100, entry_price=300.0, entry_time=datetime.now()
        )

        # Close position (submits SELL order)
        order = await broker.close_position_async("MSFT")

        # Verify order submitted
        assert order is not None
        assert order.side == OrderSide.SELL
        assert order.quantity == 100
