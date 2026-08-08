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
import math
import time
from datetime import UTC, datetime
from typing import Any

from ib_async import (
    IB,
    Contract,
    LimitOrder,
    MarketOrder,
    Stock,
    StopLimitOrder,
    StopOrder,
)
from ib_async import (
    Order as IBOrder,
)
from ib_async import (
    Trade as IBTrade,
)
from ml4t.backtest.types import Order, OrderSide, OrderStatus, OrderType, Position

logger = logging.getLogger(__name__)


class IBBroker:
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
        market_data_type: int | None = None,
    ):
        """Initialize IBBroker.

        Args:
            host: IB Gateway/TWS host (default: '127.0.0.1')
            port: IB Gateway/TWS port (default: 7497 for paper)
            client_id: Unique client ID (default: 1)
            account: IB account ID (default: use first account)
            market_data_type: IB market-data type to request after connect.
                ``None`` (default) leaves TWS at its configured default -
                use this when the account has live Level 1 subscriptions.
                ``1`` = real-time, ``2`` = frozen, ``3`` = delayed,
                ``4`` = delayed-frozen. Paper accounts without market-data
                subscriptions typically need ``3`` to avoid order rejections.
        """
        self._host = host
        self._port = port
        self._client_id = client_id
        self._account = account
        self._market_data_type = market_data_type

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
        self._snapshot_error: RuntimeError | None = None

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
        self._snapshot_error = None

        callbacks_registered = False
        try:
            # Use outer timeout wrapper like production code
            await asyncio.wait_for(
                self.ib.connectAsync(
                    host=self._host,
                    port=self._port,
                    clientId=self._client_id,
                    account=self._account or "",  # Pass account like production
                    timeout=15,
                ),
                timeout=20,  # Outer timeout wrapper
            )
        except (TimeoutError, ConnectionRefusedError) as e:
            logger.error(f"IBBroker: Connection failed: {e}")
            raise
        except Exception:
            logger.exception("IBBroker: Unexpected error during connect")
            raise

        try:
            accounts = self.ib.managedAccounts()
            if not isinstance(accounts, list) or not accounts:
                raise RuntimeError("IB returned no managed account identity")
            if self._account is None:
                self._account = accounts[0]
            elif self._account not in accounts:
                raise RuntimeError(f"Configured IB account {self._account!r} is not managed")

            # Subscribe before sync so updates cannot be missed between the snapshot and stream.
            self.ib.orderStatusEvent += self._on_order_status
            self.ib.positionEvent += self._on_position
            callbacks_registered = True

            if self._market_data_type is not None:
                self.ib.reqMarketDataType(self._market_data_type)
                logger.info(
                    "IBBroker: market_data_type=%d "
                    "(1=realtime, 2=frozen, 3=delayed, 4=delayed-frozen)",
                    self._market_data_type,
                )

            await self._sync_positions()
            await self._sync_orders()
        except Exception:
            if callbacks_registered:
                try:
                    self.ib.orderStatusEvent -= self._on_order_status
                    self.ib.positionEvent -= self._on_position
                except Exception:
                    logger.exception("IBBroker: Failed to remove callbacks after connect failure")
            self.ib.disconnect()
            self._connected = False
            self._snapshot_error = RuntimeError("IB initial broker snapshot is unavailable")
            raise

        self._snapshot_error = None
        self._connected = True
        logger.info("IBBroker: Connected successfully, account=%s", self._account)

    async def disconnect(self) -> None:
        """Disconnect from IB."""
        if self._connected:
            self.ib.orderStatusEvent -= self._on_order_status
            self.ib.positionEvent -= self._on_position
            self.ib.disconnect()
            self._connected = False
            # Give time for socket cleanup to prevent zombie connections
            await asyncio.sleep(0.1)
            logger.info("IBBroker: Disconnected")

    @property
    def is_connected(self) -> bool:
        """Check if connected to IB."""
        return self._connected and self.ib.isConnected()

    async def is_connected_async(self) -> bool:
        """Return current adapter and vendor connection state."""
        return bool(self.is_connected)

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
        self._raise_snapshot_error()
        async with self._position_lock:
            return dict(self._positions)

    async def get_position_async(self, asset: str) -> Position | None:
        """Return one position from the synchronized adapter snapshot."""
        self._raise_snapshot_error()
        async with self._position_lock:
            return self._positions.get(asset.upper())

    async def get_pending_orders_async(self, asset: str | None = None) -> list[Order]:
        """Return pending orders, optionally filtered by asset."""
        self._raise_snapshot_error()
        orders = list(self._pending_orders.values())
        if asset is None:
            return orders
        normalized = asset.upper()
        return [order for order in orders if order.asset.upper() == normalized]

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
                return self._validate_account_metric(av.value, "NetLiquidation")
        raise RuntimeError("IB NetLiquidation is unavailable for the configured account")

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
                return self._validate_account_metric(av.value, "AvailableFunds")
        raise RuntimeError("IB AvailableFunds is unavailable for the configured account")

    def _raise_snapshot_error(self) -> None:
        if self._snapshot_error is not None:
            raise RuntimeError(str(self._snapshot_error)) from self._snapshot_error

    @staticmethod
    def _validate_account_metric(value: Any, name: str) -> float:
        try:
            metric = float(value)
        except (TypeError, ValueError) as error:
            raise RuntimeError(f"IB {name} is not numeric") from error
        if not math.isfinite(metric) or metric < 0:
            raise RuntimeError(f"IB {name} must be finite and non-negative")
        return metric

    async def submit_order_async(
        self,
        asset: str,
        quantity: float,
        side: OrderSide | None = None,
        order_type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
        stop_price: float | None = None,
        **kwargs: Any,
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
        outside_rth = bool(kwargs.get("outsideRth", False))
        if order_type == OrderType.MOC and outside_rth:
            raise ValueError("IB MOC orders do not support outsideRth=True")
        ib_order = self._create_ib_order(
            action, quantity, order_type, limit_price, stop_price, outside_rth
        )

        # Submit atomically with lock
        async with self._order_lock:
            self._order_counter += 1
            order_id = f"ML4T-{self._order_counter}"

            # Place order with IB
            try:
                trade = self.ib.placeOrder(contract, ib_order)
            except Exception as e:
                # Surface a clear error; nothing has been tracked yet, so state stays consistent.
                raise RuntimeError(f"IBBroker: failed to place order for {asset}: {e}") from e

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
                created_at=datetime.now(UTC),
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

    async def replace_order_async(
        self,
        order_id: str,
        *,
        quantity: float | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> Order:
        """Replace a pending order via cancel-and-resubmit."""
        original = self._pending_orders.get(order_id)
        if original is None:
            raise RuntimeError(f"Order {order_id} not found in pending orders")

        replacement_quantity = original.quantity if quantity is None else quantity
        replacement_limit = original.limit_price if limit_price is None else limit_price
        replacement_stop = original.stop_price if stop_price is None else stop_price

        cancelled = await self.cancel_order_async(order_id)
        if not cancelled:
            raise RuntimeError(f"Failed to cancel order {order_id} before replacement")

        return await self.submit_order_async(
            asset=original.asset,
            quantity=replacement_quantity,
            side=original.side,
            order_type=original.order_type,
            limit_price=replacement_limit,
            stop_price=replacement_stop,
        )

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
        outside_rth: bool = False,
    ) -> Any:
        """Create IB order object.

        Args:
            action: 'BUY' or 'SELL'
            quantity: Number of shares
            order_type: Market, limit, stop, or stop-limit
            limit_price: Limit price for limit orders
            stop_price: Stop price for stop orders
            outside_rth: Allow the order to fill outside regular trading hours

        Returns:
            IB order object (MarketOrder, LimitOrder, etc.)

        Raises:
            ValueError: If order type is unsupported
        """
        if order_type == OrderType.MARKET:
            order = MarketOrder(action, quantity)
        elif order_type == OrderType.LIMIT:
            if limit_price is None:
                raise ValueError("limit_price required for LIMIT orders")
            order = LimitOrder(action, quantity, limit_price)
        elif order_type == OrderType.STOP:
            if stop_price is None:
                raise ValueError("stop_price required for STOP orders")
            order = StopOrder(action, quantity, stop_price)
        elif order_type == OrderType.STOP_LIMIT:
            if limit_price is None or stop_price is None:
                raise ValueError("limit_price and stop_price required for STOP_LIMIT orders")
            order = StopLimitOrder(action, quantity, limit_price, stop_price)
        elif order_type == OrderType.MOC:
            # Market-on-close is session-bound; outside_rth is rejected upstream.
            return IBOrder(action=action, totalQuantity=quantity, orderType="MOC", tif="DAY")
        else:
            raise ValueError(f"Unsupported order type: {order_type}")

        # Honor the caller's extended-hours intent (no-op before this was set).
        order.outsideRth = outside_rth
        return order

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

        status_str = str(trade.orderStatus.status)
        try:
            filled_quantity = float(trade.orderStatus.filled or 0.0)
            filled_price = float(trade.orderStatus.avgFillPrice or 0.0)
        except (TypeError, ValueError) as error:
            self._invalidate_snapshot("IB order update contains non-numeric fill data", error)
            return
        if (
            not math.isfinite(filled_quantity)
            or filled_quantity < 0
            or filled_quantity > order.quantity
            or filled_quantity < order.filled_quantity
        ):
            if filled_quantity < order.filled_quantity:
                logger.warning(
                    "IBBroker: Ignoring out-of-order fill update for %s: %s < %s",
                    order_id,
                    filled_quantity,
                    order.filled_quantity,
                )
                return
            self._invalidate_snapshot("IB order update contains an invalid cumulative fill")
            return

        if status_str == "Filled":
            if (
                filled_quantity != order.quantity
                or not math.isfinite(filled_price)
                or filled_price <= 0
            ):
                self._invalidate_snapshot("IB filled update is incomplete or invalid")
                return
            # Order filled - update status and remove from pending
            order.status = OrderStatus.FILLED
            order.filled_price = filled_price
            order.filled_quantity = filled_quantity
            order.filled_at = datetime.now(UTC)
            del self._pending_orders[order_id]
            logger.info(f"IBBroker: Order {order_id} FILLED @ {order.filled_price}")

            # Memory leak fix: schedule cleanup of _ib_order_map entry after 1 hour
            # We delay cleanup to allow time for any late callbacks or queries
            def cleanup_ib_order(oid: int = ib_order_id) -> None:
                self._ib_order_map.pop(oid, None)

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    loop = None

            closed = False
            if loop is not None:
                is_closed = getattr(loop, "is_closed", None)
                if callable(is_closed):
                    result = is_closed()
                    if isinstance(result, bool):
                        closed = result

            if loop is not None and not closed:
                loop.call_later(3600, cleanup_ib_order)
        elif status_str in ("Cancelled", "ApiCancelled"):
            # Order cancelled - update status and cleanup immediately
            order.status = OrderStatus.CANCELLED
            if order_id in self._pending_orders:
                del self._pending_orders[order_id]
            # Memory leak fix: cleanup immediately for cancelled orders (no need to keep)
            self._ib_order_map.pop(ib_order_id, None)
            logger.info(f"IBBroker: Order {order_id} CANCELLED")
        elif status_str == "Inactive":
            order.status = OrderStatus.REJECTED
            self._pending_orders.pop(order_id, None)
            self._ib_order_map.pop(ib_order_id, None)
            logger.info("IBBroker: Order %s REJECTED", order_id)
        elif status_str in (
            "PendingSubmit",
            "ApiPending",
            "PreSubmitted",
            "Submitted",
            "PartiallyFilled",
            "PendingCancel",
        ):
            if filled_quantity > order.filled_quantity:
                order.filled_quantity = filled_quantity
                if filled_price > 0 and math.isfinite(filled_price):
                    order.filled_price = filled_price
        else:
            self._invalidate_snapshot(f"unsupported IB order status {status_str!r}")

    def _on_position(self, position: Any) -> None:
        """Handle IB position update (with lock for thread safety).

        Args:
            position: IB Position object
        """
        try:
            asset = str(position.contract.symbol).strip().upper()
            quantity = float(position.position)
            average_cost = float(position.avgCost or 0.0)
        except (AttributeError, TypeError, ValueError) as error:
            self._invalidate_snapshot("IB position update is invalid", error)
            return
        if not asset or not math.isfinite(quantity):
            self._invalidate_snapshot("IB position update has invalid identity or quantity")
            return
        if quantity != 0 and (not math.isfinite(average_cost) or average_cost <= 0):
            self._invalidate_snapshot("IB position update has invalid average cost")
            return

        # Note: This runs in the IB event loop. We update directly since
        # _positions is only read via copy in the positions property.
        if quantity != 0:
            # IB position events carry only average cost, not a live mark (unlike
            # Alpaca). avgCost seeds current_price as the initial reference for the
            # risk layer's price-deviation check; the live data feed overwrites it
            # with the real mark on the first tick.
            self._positions[asset] = Position(
                asset=asset,
                quantity=quantity,
                entry_price=average_cost,
                entry_time=datetime.now(UTC),
                current_price=average_cost,
            )
        elif asset in self._positions:
            del self._positions[asset]

    async def _sync_positions(self) -> None:
        """Sync positions from IB."""
        positions = await self.ib.reqPositionsAsync()
        if not isinstance(positions, list):
            raise RuntimeError("IB positions snapshot must be a list")
        original_positions = self._positions
        candidate_positions: dict[str, Position] = {}
        self._positions = candidate_positions
        try:
            for pos in positions:
                self._on_position(pos)
                self._raise_snapshot_error()
        except Exception:
            self._positions = original_positions
            raise
        logger.info(f"IBBroker: Synced {len(self._positions)} positions")

    async def _sync_orders(self) -> None:
        """Sync open orders from IB.

        TASK-013: Full order sync implementation.
        """
        trades = self.ib.openTrades()
        if not isinstance(trades, list):
            raise RuntimeError("IB open-orders snapshot must be a list")
        candidate_orders: dict[str, Order] = {}
        candidate_map: dict[int, tuple[str, float]] = {}
        for trade in trades:
            if trade.orderStatus.status in (
                "PendingSubmit",
                "ApiPending",
                "PreSubmitted",
                "Submitted",
                "PendingCancel",
            ):
                # Generate order ID for existing order
                self._order_counter += 1
                order_id = f"ML4T-{self._order_counter}"

                # Determine order type from IB order
                order_type = OrderType.MARKET
                ib_order_type = str(getattr(trade.order, "orderType", "")).upper()
                if ib_order_type == "MOC":
                    order_type = OrderType.MOC
                elif hasattr(trade.order, "lmtPrice") and trade.order.lmtPrice:
                    if hasattr(trade.order, "auxPrice") and trade.order.auxPrice:
                        order_type = OrderType.STOP_LIMIT
                    else:
                        order_type = OrderType.LIMIT
                elif hasattr(trade.order, "auxPrice") and trade.order.auxPrice:
                    order_type = OrderType.STOP

                # Create our order object
                asset = str(trade.contract.symbol).strip().upper()
                quantity = float(trade.order.totalQuantity)
                action = str(trade.order.action).upper()
                limit_value = trade.order.lmtPrice
                stop_value = trade.order.auxPrice
                limit_price = (
                    float(limit_value)
                    if order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT)
                    and limit_value is not None
                    else None
                )
                stop_price = (
                    float(stop_value)
                    if order_type in (OrderType.STOP, OrderType.STOP_LIMIT)
                    and stop_value is not None
                    else None
                )
                venue_id = int(trade.order.orderId)
                if (
                    not asset
                    or action not in ("BUY", "SELL")
                    or not math.isfinite(quantity)
                    or quantity <= 0
                    or (
                        order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT)
                        and limit_price is None
                    )
                    or (order_type in (OrderType.STOP, OrderType.STOP_LIMIT) and stop_price is None)
                    or (
                        limit_price is not None
                        and (not math.isfinite(limit_price) or limit_price <= 0)
                    )
                    or (
                        stop_price is not None
                        and (not math.isfinite(stop_price) or stop_price <= 0)
                    )
                    or venue_id in candidate_map
                ):
                    raise RuntimeError("IB open-orders snapshot contains an invalid order")
                candidate_orders[order_id] = Order(
                    asset=asset,
                    side=OrderSide.BUY if action == "BUY" else OrderSide.SELL,
                    quantity=quantity,
                    order_type=order_type,
                    limit_price=limit_price,
                    stop_price=stop_price,
                    order_id=order_id,
                    status=OrderStatus.PENDING,
                    created_at=datetime.now(UTC),
                )
                candidate_map[venue_id] = (order_id, time.time())

        async with self._order_lock:
            self._pending_orders = candidate_orders
            self._ib_order_map = candidate_map

        logger.info(f"IBBroker: Synced {len(self._pending_orders)} open orders")

    def _invalidate_snapshot(self, message: str, cause: Exception | None = None) -> None:
        error = RuntimeError(message)
        if cause is not None:
            error.__cause__ = cause
        self._snapshot_error = error
        self._connected = False
        logger.error("IBBroker: %s", message)
