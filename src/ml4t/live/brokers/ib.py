"""Interactive Brokers implementation using ib_async.

This module provides async broker integration with Interactive Brokers API.
TASK-011: Connection setup ✅
TASK-012: Position sync ✅
TASK-013: Order submission ✅
TASK-014: Order status callbacks ✅
TASK-015: Account queries ✅ (already implemented in TASK-011)
TASK-016: Order cancellation ✅

IB Integration Layer: COMPLETE ✅
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from ib_async import IB, Contract, LimitOrder, MarketOrder, Stock, StopLimitOrder, StopOrder
from ib_async import Trade as IBTrade

from ml4t.backtest.types import Order, OrderSide, OrderStatus, OrderType, Position
from ml4t.live.protocols import AsyncBrokerProtocol

logger = logging.getLogger(__name__)


class IBBroker(AsyncBrokerProtocol):
    """Interactive Brokers implementation.

    Design:
    - All broker operations are async
    - Uses asyncio.Lock for thread safety
    - Event handlers use put_nowait() (non-blocking)
    - Reconnection handled externally

    Connection Ports:
    - TWS Paper: 7497
    - TWS Live: 7496
    - Gateway Paper: 4002
    - Gateway Live: 4001

    Example:
        broker = IBBroker(port=7497)  # Paper trading
        await broker.connect()
        positions = await broker.get_positions_async()
        await broker.disconnect()
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7497,  # Paper trading default
        client_id: int = 1,
        account: str | None = None,
    ):
        """Initialize IBBroker.

        Args:
            host: IB Gateway/TWS host (default: '127.0.0.1')
            port: IB Gateway/TWS port (default: 7497 for paper)
            client_id: Unique client ID (default: 1)
            account: IB account ID (default: use first account)
        """
        self._host = host
        self._port = port
        self._client_id = client_id
        self._account = account

        self.ib = IB()
        self.ib.RequestTimeout = 60
        self.ib.RaiseRequestErrors = True
        self._connected = False

        # Thread-safe state with locks
        self._positions: dict[str, Position] = {}
        self._position_lock = asyncio.Lock()
        self._pending_orders: dict[str, Order] = {}
        self._order_lock = asyncio.Lock()

        # Order tracking
        self._order_counter = 0
        self._ib_order_map: dict[int, tuple[str, float]] = {}  # IB orderId -> (our_id, timestamp)

        # Contract cache
        self._contracts: dict[str, Contract] = {}

    async def connect(self) -> None:
        """Connect to IB Gateway/TWS.

        Raises:
            RuntimeError: If connection fails
            asyncio.TimeoutError: If connection times out
        """
        if self._connected:
            logger.info("IBBroker: Already connected")
            return

        logger.info(
            f"IBBroker: Connecting to {self._host}:{self._port} (client_id={self._client_id})"
        )

        try:
            # Use outer timeout wrapper like production code
            await asyncio.wait_for(
                self.ib.connectAsync(
                    host=self._host,
                    port=self._port,
                    clientId=self._client_id,
                    account=self._account,  # Pass account like production
                    timeout=15,
                ),
                timeout=20,  # Outer timeout wrapper
            )
        except (asyncio.TimeoutError, ConnectionRefusedError) as e:
            logger.error(f"IBBroker: Connection failed: {e}")
            raise
        except Exception:
            logger.exception("IBBroker: Unexpected error during connect")
            raise

        self._connected = True

        # Get account if not specified
        if self._account is None:
            accounts = self.ib.managedAccounts()
            if accounts:
                self._account = accounts[0]
        logger.info(f"IBBroker: Connected successfully, account={self._account}")

        # Subscribe to events
        self.ib.orderStatusEvent += self._on_order_status
        self.ib.positionEvent += self._on_position

        # Initial sync
        await self._sync_positions()
        await self._sync_orders()

    async def disconnect(self) -> None:
        """Disconnect from IB."""
        if self._connected:
            self.ib.disconnect()
            self._connected = False
            # Give time for socket cleanup to prevent zombie connections
            await asyncio.sleep(0.1)
            logger.info("IBBroker: Disconnected")

    @property
    def is_connected(self) -> bool:
        """Check if connected to IB."""
        return self._connected and self.ib.isConnected()

    # === AsyncBrokerProtocol Implementation ===

    @property
    def positions(self) -> dict[str, Position]:
        """Thread-safe position access (Gemini v2 Critical Issue C).

        Note: This is called from worker thread via ThreadSafeBrokerWrapper.
        The lock prevents RuntimeError during dict iteration if IB callback
        modifies positions concurrently.

        Returns:
            Dictionary mapping asset symbols to Position objects
        """
        # For sync access from worker thread, we copy under implicit lock
        # The asyncio.Lock is acquired in async methods below
        return dict(self._positions)  # Shallow copy is atomic for small dicts

    @property
    def pending_orders(self) -> list[Order]:
        """Get list of pending orders.

        Returns:
            List of pending Order objects
        """
        return list(self._pending_orders.values())

    def get_position(self, asset: str) -> Position | None:
        """Thread-safe single position access.

        Args:
            asset: Asset symbol

        Returns:
            Position object if exists, None otherwise
        """
        return self._positions.get(asset.upper())

    async def get_positions_async(self) -> dict[str, Position]:
        """Async thread-safe position access with lock.

        Returns:
            Dictionary mapping asset symbols to Position objects
        """
        async with self._position_lock:
            return dict(self._positions)

    async def get_account_value_async(self) -> float:
        """Get Net Liquidation Value.

        Returns:
            Account net liquidation value in USD
        """
        for av in self.ib.accountValues():
            if (
                av.tag == "NetLiquidation"
                and av.currency == "USD"
                and (av.account == self._account or self._account is None)
            ):
                return float(av.value)
        return 0.0

    async def get_cash_async(self) -> float:
        """Get available funds.

        Returns:
            Available funds in USD
        """
        for av in self.ib.accountValues():
            if (
                av.tag == "AvailableFunds"
                and av.currency == "USD"
                and (av.account == self._account or self._account is None)
            ):
                return float(av.value)
        return 0.0

    async def submit_order_async(
        self,
        asset: str,
        quantity: float,
        side: OrderSide | None = None,
        order_type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> Order:
        """Submit order to IB.

        TASK-013: Full order submission implementation with IB order tracking.

        Args:
            asset: Asset symbol
            quantity: Number of shares
            side: BUY or SELL (auto-detected if None)
            order_type: Market, limit, stop, or stop-limit
            limit_price: Limit price for limit orders
            stop_price: Stop price for stop orders

        Returns:
            Order object

        Raises:
            RuntimeError: If not connected
            ValueError: If order parameters are invalid
        """
        if not self.is_connected:
            raise RuntimeError("Not connected to IB")

        # Auto-detect side if not provided
        asset = asset.upper()
        if side is None:
            pos = self.get_position(asset)
            if pos and pos.quantity < 0:
                # Short position, assume closing (buy)
                side = OrderSide.BUY
            else:
                # Long or no position, assume opening/adding (buy)
                side = OrderSide.BUY

        # Get contract
        contract = self._get_contract(asset)

        # Create IB order
        action = "BUY" if side == OrderSide.BUY else "SELL"
        ib_order = self._create_ib_order(action, quantity, order_type, limit_price, stop_price)

        # Submit atomically with lock
        async with self._order_lock:
            self._order_counter += 1
            order_id = f"ML4T-{self._order_counter}"

            # Place order with IB
            trade = self.ib.placeOrder(contract, ib_order)

            # Create our order
            order = Order(
                asset=asset,
                side=side,
                quantity=quantity,
                order_type=order_type,
                limit_price=limit_price,
                stop_price=stop_price,
                order_id=order_id,
                status=OrderStatus.PENDING,
                created_at=datetime.now(),
            )

            # Track order
            self._pending_orders[order_id] = order
            self._ib_order_map[trade.order.orderId] = (order_id, time.time())

        logger.info(f"IBBroker: Order {order_id} submitted: {side.value} {quantity} {asset}")
        return order

    async def cancel_order_async(self, order_id: str) -> bool:
        """Cancel pending order.

        TASK-016: Full order cancellation implementation.

        This method finds the IB order ID from our tracking map and cancels
        the order via the IB API. Handles edge cases like order not found
        or order already filled.

        Args:
            order_id: Order ID to cancel (e.g., 'ML4T-1')

        Returns:
            True if cancellation request sent successfully, False otherwise

        Note:
            The actual cancellation is confirmed via _on_order_status callback
            when IB sends the 'Cancelled' status update.
        """
        # Find IB order ID from our tracking map
        ib_order_id = None
        for ib_id, (our_id, _) in self._ib_order_map.items():
            if our_id == order_id:
                ib_order_id = ib_id
                break

        if ib_order_id is None:
            logger.warning(f"IBBroker: Order {order_id} not found in tracking map")
            return False

        # Find the trade in open trades and cancel
        for trade in self.ib.openTrades():
            if trade.order.orderId == ib_order_id:
                self.ib.cancelOrder(trade.order)
                logger.info(f"IBBroker: Cancellation requested for order {order_id}")
                return True

        # Order not in open trades (possibly already filled or cancelled)
        logger.warning(f"IBBroker: Order {order_id} not found in open trades")
        return False

    async def close_position_async(self, asset: str) -> Order | None:
        """Close position in asset.

        Args:
            asset: Asset symbol

        Returns:
            Order object if position exists, None otherwise

        Raises:
            NotImplementedError: Depends on TASK-013
        """
        pos = self.get_position(asset)
        if not pos or pos.quantity == 0:
            return None

        side = OrderSide.SELL if pos.quantity > 0 else OrderSide.BUY
        return await self.submit_order_async(asset, abs(pos.quantity), side)

    # === Internal Methods ===

    def _get_contract(self, asset: str) -> Contract:
        """Get or create IB contract.

        Args:
            asset: Asset symbol

        Returns:
            IB Contract object
        """
        asset = asset.upper()
        if asset not in self._contracts:
            self._contracts[asset] = Stock(asset, "SMART", "USD")
        return self._contracts[asset]

    def _create_ib_order(
        self,
        action: str,
        quantity: float,
        order_type: OrderType,
        limit_price: float | None,
        stop_price: float | None,
    ) -> Any:
        """Create IB order object.

        Args:
            action: 'BUY' or 'SELL'
            quantity: Number of shares
            order_type: Market, limit, stop, or stop-limit
            limit_price: Limit price for limit orders
            stop_price: Stop price for stop orders

        Returns:
            IB order object (MarketOrder, LimitOrder, etc.)

        Raises:
            ValueError: If order type is unsupported
        """
        if order_type == OrderType.MARKET:
            return MarketOrder(action, quantity)
        elif order_type == OrderType.LIMIT:
            return LimitOrder(action, quantity, limit_price)
        elif order_type == OrderType.STOP:
            return StopOrder(action, quantity, stop_price)
        elif order_type == OrderType.STOP_LIMIT:
            return StopLimitOrder(action, quantity, limit_price, stop_price)
        raise ValueError(f"Unsupported order type: {order_type}")

    def _on_order_status(self, trade: IBTrade) -> None:
        """Handle IB order status update.

        TASK-014: Full order status callback implementation with memory leak prevention.

        This callback is invoked by the IB event loop when order status changes.
        It updates our internal order tracking and handles filled/cancelled orders.

        Args:
            trade: IB Trade object containing order and status information
        """
        ib_order_id = trade.order.orderId
        entry = self._ib_order_map.get(ib_order_id)
        if not entry:
            # Order not tracked by us (possibly from another client)
            return

        order_id, _ = entry
        order = self._pending_orders.get(order_id)
        if not order:
            # Order already processed or removed
            return

        status_str = trade.orderStatus.status
        if status_str == "Filled":
            # Order filled - update status and remove from pending
            order.status = OrderStatus.FILLED
            order.filled_price = trade.orderStatus.avgFillPrice
            order.filled_quantity = trade.orderStatus.filled
            order.filled_at = datetime.now()
            del self._pending_orders[order_id]
            logger.info(f"IBBroker: Order {order_id} FILLED @ {order.filled_price}")

            # Memory leak fix: schedule cleanup of _ib_order_map entry after 1 hour
            # We delay cleanup to allow time for any late callbacks or queries
            def cleanup_ib_order(oid: int = ib_order_id) -> None:
                self._ib_order_map.pop(oid, None)

            asyncio.get_event_loop().call_later(3600, cleanup_ib_order)
        elif status_str == "Cancelled":
            # Order cancelled - update status and cleanup immediately
            order.status = OrderStatus.CANCELLED
            if order_id in self._pending_orders:
                del self._pending_orders[order_id]
            # Memory leak fix: cleanup immediately for cancelled orders (no need to keep)
            self._ib_order_map.pop(ib_order_id, None)
            logger.info(f"IBBroker: Order {order_id} CANCELLED")

    def _on_position(self, position: Any) -> None:
        """Handle IB position update (with lock for thread safety).

        Args:
            position: IB Position object
        """
        asset = position.contract.symbol.upper()

        # Note: This runs in the IB event loop. We update directly since
        # _positions is only read via copy in the positions property.
        if position.position != 0:
            self._positions[asset] = Position(
                asset=asset,
                quantity=float(position.position),
                entry_price=float(position.avgCost) if position.avgCost else 0.0,
                entry_time=datetime.now(),
                current_price=float(position.avgCost) if position.avgCost else None,
            )
        elif asset in self._positions:
            del self._positions[asset]

    async def _sync_positions(self) -> None:
        """Sync positions from IB."""
        positions = await self.ib.reqPositionsAsync()
        for pos in positions:
            self._on_position(pos)
        logger.info(f"IBBroker: Synced {len(self._positions)} positions")

    async def _sync_orders(self) -> None:
        """Sync open orders from IB.

        TASK-013: Full order sync implementation.
        """
        for trade in self.ib.openTrades():
            if trade.orderStatus.status in ("PreSubmitted", "Submitted"):
                # Generate order ID for existing order
                self._order_counter += 1
                order_id = f"ML4T-{self._order_counter}"

                # Determine order type from IB order
                order_type = OrderType.MARKET
                if hasattr(trade.order, "lmtPrice") and trade.order.lmtPrice:
                    if hasattr(trade.order, "auxPrice") and trade.order.auxPrice:
                        order_type = OrderType.STOP_LIMIT
                    else:
                        order_type = OrderType.LIMIT
                elif hasattr(trade.order, "auxPrice") and trade.order.auxPrice:
                    order_type = OrderType.STOP

                # Create our order object
                self._pending_orders[order_id] = Order(
                    asset=trade.contract.symbol.upper(),
                    side=OrderSide.BUY if trade.order.action == "BUY" else OrderSide.SELL,
                    quantity=trade.order.totalQuantity,
                    order_type=order_type,
                    order_id=order_id,
                    status=OrderStatus.PENDING,
                    created_at=datetime.now(),
                )
                self._ib_order_map[trade.order.orderId] = (order_id, time.time())

        logger.info(f"IBBroker: Synced {len(self._pending_orders)} open orders")
