"""Alpaca Markets broker implementation using alpaca-py.

This module provides async broker integration with Alpaca Markets API.
Supports both stocks and crypto trading.

Design (matching IBBroker patterns):
- All broker operations are async
- Uses asyncio.Lock for thread safety
- WebSocket stream for real-time order updates
- REST API for account/position queries and order submission
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from alpaca.trading.client import TradingClient
from alpaca.trading.stream import TradingStream
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopLimitOrderRequest,
    StopOrderRequest,
)
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.enums import OrderStatus as AlpacaOrderStatus
from alpaca.trading.enums import QueryOrderStatus, TimeInForce

from ml4t.backtest.types import Order, OrderSide, OrderStatus, OrderType, Position
from ml4t.live.protocols import AsyncBrokerProtocol

logger = logging.getLogger(__name__)


class AlpacaBroker(AsyncBrokerProtocol):
    """Alpaca Markets broker implementation.

    Design (matching IBBroker patterns):
    - All broker operations are async
    - Uses asyncio.Lock for thread safety
    - WebSocket stream for real-time order updates
    - REST API for account/position queries and order submission

    Paper vs Live:
    - paper=True (default): Uses paper trading endpoint
    - paper=False: Uses live trading endpoint (USE WITH CAUTION)

    Example:
        broker = AlpacaBroker(
            api_key='PKXXXXXXXX',
            secret_key='XXXXXXXXXX',
            paper=True,  # Always start with paper trading!
        )
        await broker.connect()
        positions = await broker.get_positions_async()
        await broker.disconnect()
    """

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        paper: bool = True,  # Paper trading by default (SAFETY)
    ):
        """Initialize AlpacaBroker.

        Args:
            api_key: Alpaca API key (from https://app.alpaca.markets)
            secret_key: Alpaca secret key
            paper: Use paper trading endpoint (default: True)
        """
        self._api_key = api_key
        self._secret_key = secret_key
        self._paper = paper

        # Clients (created in connect())
        self._trading_client: TradingClient | None = None
        self._trading_stream: TradingStream | None = None
        self._stream_task: asyncio.Task | None = None

        # Connection state
        self._connected = False

        # Thread-safe state with locks (matching IBBroker pattern)
        self._positions: dict[str, Position] = {}
        self._position_lock = asyncio.Lock()
        self._pending_orders: dict[str, Order] = {}
        self._order_lock = asyncio.Lock()

        # Order tracking (matching IBBroker pattern)
        self._order_counter = 0
        # Alpaca order ID (UUID string) -> (our_id, timestamp)
        self._alpaca_order_map: dict[str, tuple[str, float]] = {}

    async def connect(self) -> None:
        """Connect to Alpaca and sync initial state.

        Steps:
        1. Create TradingClient (REST)
        2. Create TradingStream (WebSocket)
        3. Verify connection by fetching account
        4. Register trade update callback
        5. Sync positions and open orders
        6. Start WebSocket stream for order updates

        Raises:
            RuntimeError: If connection fails
        """
        if self._connected:
            logger.info("AlpacaBroker: Already connected")
            return

        mode = "paper" if self._paper else "LIVE"
        logger.info(f"AlpacaBroker: Connecting ({mode} trading)")

        try:
            # Create REST client
            self._trading_client = TradingClient(
                api_key=self._api_key,
                secret_key=self._secret_key,
                paper=self._paper,
            )

            # Verify connection by fetching account
            account = self._trading_client.get_account()
            logger.info(
                f"AlpacaBroker: Account verified - "
                f"equity=${float(account.equity):,.2f}, "
                f"cash=${float(account.cash):,.2f}"
            )

            # Create WebSocket stream for order updates
            self._trading_stream = TradingStream(
                api_key=self._api_key,
                secret_key=self._secret_key,
                paper=self._paper,
            )

            # Subscribe to trade updates BEFORE initial sync (IBBroker pattern)
            self._trading_stream.subscribe_trade_updates(self._on_trade_update)

            # Initial sync
            await self._sync_positions()
            await self._sync_orders()

            # Start stream in background task
            self._stream_task = asyncio.create_task(self._run_trading_stream())

            self._connected = True
            logger.info("AlpacaBroker: Connected successfully")

        except Exception as e:
            logger.error(f"AlpacaBroker: Connection failed: {e}")
            raise RuntimeError(f"Failed to connect to Alpaca: {e}") from e

    async def disconnect(self) -> None:
        """Disconnect from Alpaca."""
        if not self._connected:
            return

        # Cancel stream task
        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass

        # Close stream
        if self._trading_stream:
            try:
                self._trading_stream.stop()
            except Exception as e:
                logger.warning(f"AlpacaBroker: Error stopping stream: {e}")

        self._connected = False
        self._trading_client = None
        self._trading_stream = None
        self._stream_task = None

        logger.info("AlpacaBroker: Disconnected")

    @property
    def is_connected(self) -> bool:
        """Check if connected to Alpaca."""
        return self._connected and self._trading_client is not None

    # === AsyncBrokerProtocol Implementation ===

    @property
    def positions(self) -> dict[str, Position]:
        """Thread-safe position access (shallow copy).

        Note: This is called from worker thread via ThreadSafeBrokerWrapper.
        The shallow copy prevents RuntimeError during dict iteration.

        Returns:
            Dictionary mapping asset symbols to Position objects
        """
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
            asset: Asset symbol (e.g., 'AAPL' or 'BTC/USD')

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
        """Get portfolio value (equity).

        Returns:
            Total account equity in USD
        """
        if not self._trading_client:
            return 0.0

        account = self._trading_client.get_account()
        return float(account.equity)

    async def get_cash_async(self) -> float:
        """Get available cash.

        Returns:
            Available cash in USD
        """
        if not self._trading_client:
            return 0.0

        account = self._trading_client.get_account()
        return float(account.cash)

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
        """Submit order to Alpaca.

        Args:
            asset: Asset symbol (e.g., 'AAPL' or 'BTC/USD')
            quantity: Number of shares/units
            side: BUY or SELL (auto-detected from quantity sign if None)
            order_type: Market, limit, stop, or stop-limit
            limit_price: Limit price for limit orders
            stop_price: Stop price for stop orders
            **kwargs: Additional parameters (ignored)

        Returns:
            Order object

        Raises:
            RuntimeError: If not connected
            ValueError: If order parameters are invalid
        """
        if not self.is_connected or not self._trading_client:
            raise RuntimeError("Not connected to Alpaca")

        # Normalize asset symbol
        asset = asset.upper()

        # Auto-detect side from quantity sign if not provided
        if side is None:
            side = OrderSide.BUY if quantity > 0 else OrderSide.SELL
        qty = abs(quantity)

        # Create order request
        order_request = self._create_order_request(
            asset, qty, side, order_type, limit_price, stop_price
        )

        # Submit atomically with lock (IBBroker pattern)
        async with self._order_lock:
            self._order_counter += 1
            order_id = f"ML4T-{self._order_counter}"

            # Submit to Alpaca
            alpaca_order = self._trading_client.submit_order(order_request)

            # Create our order object
            order = Order(
                asset=asset,
                side=side,
                quantity=qty,
                order_type=order_type,
                limit_price=limit_price,
                stop_price=stop_price,
                order_id=order_id,
                status=self._map_order_status(alpaca_order.status),
                created_at=alpaca_order.created_at or datetime.now(timezone.utc),
            )

            # Track order
            self._pending_orders[order_id] = order
            self._alpaca_order_map[str(alpaca_order.id)] = (order_id, time.time())

        logger.info(f"AlpacaBroker: Order {order_id} submitted: {side.value} {qty} {asset}")
        return order

    async def cancel_order_async(self, order_id: str) -> bool:
        """Cancel pending order.

        Args:
            order_id: Order ID to cancel (e.g., 'ML4T-1')

        Returns:
            True if cancellation request sent successfully, False otherwise
        """
        if not self._trading_client:
            return False

        # Find Alpaca order ID from our tracking map
        alpaca_order_id = None
        for alpaca_id, (our_id, _) in self._alpaca_order_map.items():
            if our_id == order_id:
                alpaca_order_id = alpaca_id
                break

        if alpaca_order_id is None:
            logger.warning(f"AlpacaBroker: Order {order_id} not found in tracking map")
            return False

        try:
            self._trading_client.cancel_order_by_id(alpaca_order_id)
            logger.info(f"AlpacaBroker: Cancellation requested for order {order_id}")
            return True
        except Exception as e:
            logger.warning(f"AlpacaBroker: Failed to cancel order {order_id}: {e}")
            return False

    async def close_position_async(self, asset: str) -> Order | None:
        """Close position in asset.

        Args:
            asset: Asset symbol

        Returns:
            Order object if position exists, None otherwise
        """
        pos = self.get_position(asset)
        if not pos or pos.quantity == 0:
            return None

        side = OrderSide.SELL if pos.quantity > 0 else OrderSide.BUY
        return await self.submit_order_async(asset, abs(pos.quantity), side)

    # === Internal Methods ===

    def _create_order_request(
        self,
        asset: str,
        quantity: float,
        side: OrderSide,
        order_type: OrderType,
        limit_price: float | None,
        stop_price: float | None,
    ) -> MarketOrderRequest | LimitOrderRequest | StopOrderRequest | StopLimitOrderRequest:
        """Create Alpaca order request.

        Args:
            asset: Asset symbol
            quantity: Number of shares
            side: BUY or SELL
            order_type: Market, limit, stop, or stop-limit
            limit_price: Limit price for limit orders
            stop_price: Stop price for stop orders

        Returns:
            Alpaca order request object

        Raises:
            ValueError: If order type is unsupported
        """
        alpaca_side = AlpacaOrderSide.BUY if side == OrderSide.BUY else AlpacaOrderSide.SELL

        # Check if crypto (symbol contains '/')
        is_crypto = "/" in asset

        # Use appropriate time in force
        # Crypto supports GTC, stocks use DAY
        tif = TimeInForce.GTC if is_crypto else TimeInForce.DAY

        if order_type == OrderType.MARKET:
            return MarketOrderRequest(
                symbol=asset,
                qty=quantity,
                side=alpaca_side,
                time_in_force=tif,
            )
        elif order_type == OrderType.LIMIT:
            if limit_price is None:
                raise ValueError("Limit price required for limit orders")
            return LimitOrderRequest(
                symbol=asset,
                qty=quantity,
                side=alpaca_side,
                limit_price=limit_price,
                time_in_force=tif,
            )
        elif order_type == OrderType.STOP:
            if stop_price is None:
                raise ValueError("Stop price required for stop orders")
            return StopOrderRequest(
                symbol=asset,
                qty=quantity,
                side=alpaca_side,
                stop_price=stop_price,
                time_in_force=tif,
            )
        elif order_type == OrderType.STOP_LIMIT:
            if limit_price is None or stop_price is None:
                raise ValueError("Both limit and stop price required for stop-limit orders")
            return StopLimitOrderRequest(
                symbol=asset,
                qty=quantity,
                side=alpaca_side,
                limit_price=limit_price,
                stop_price=stop_price,
                time_in_force=tif,
            )
        else:
            raise ValueError(f"Unsupported order type: {order_type}")

    def _map_order_status(self, alpaca_status: AlpacaOrderStatus) -> OrderStatus:
        """Map Alpaca order status to ML4T order status.

        Args:
            alpaca_status: Alpaca order status enum

        Returns:
            ML4T OrderStatus enum
        """
        status_map = {
            AlpacaOrderStatus.NEW: OrderStatus.PENDING,
            AlpacaOrderStatus.ACCEPTED: OrderStatus.PENDING,
            AlpacaOrderStatus.PENDING_NEW: OrderStatus.PENDING,
            AlpacaOrderStatus.PARTIALLY_FILLED: OrderStatus.PENDING,
            AlpacaOrderStatus.FILLED: OrderStatus.FILLED,
            AlpacaOrderStatus.CANCELED: OrderStatus.CANCELLED,
            AlpacaOrderStatus.EXPIRED: OrderStatus.CANCELLED,
            AlpacaOrderStatus.REJECTED: OrderStatus.REJECTED,
            AlpacaOrderStatus.PENDING_CANCEL: OrderStatus.PENDING,
            AlpacaOrderStatus.PENDING_REPLACE: OrderStatus.PENDING,
            AlpacaOrderStatus.REPLACED: OrderStatus.CANCELLED,
            AlpacaOrderStatus.STOPPED: OrderStatus.PENDING,
            AlpacaOrderStatus.SUSPENDED: OrderStatus.PENDING,
        }
        return status_map.get(alpaca_status, OrderStatus.PENDING)

    async def _on_trade_update(self, data: Any) -> None:
        """Handle WebSocket trade update.

        This callback is invoked when order status changes via WebSocket.
        It updates our internal order tracking and handles filled/cancelled orders.

        Args:
            data: Trade update data from Alpaca WebSocket
        """
        try:
            event = data.event
            order_data = data.order
            alpaca_order_id = str(order_data.id)

            entry = self._alpaca_order_map.get(alpaca_order_id)
            if not entry:
                # Order not tracked by us
                return

            order_id, _ = entry
            order = self._pending_orders.get(order_id)
            if not order:
                # Order already processed
                return

            logger.debug(f"AlpacaBroker: Trade update - {event} for order {order_id}")

            if event == "fill":
                # Order filled - update status and remove from pending
                order.status = OrderStatus.FILLED
                order.filled_price = (
                    float(order_data.filled_avg_price) if order_data.filled_avg_price else 0.0
                )
                order.filled_quantity = (
                    float(order_data.filled_qty) if order_data.filled_qty else order.quantity
                )
                order.filled_at = datetime.now(timezone.utc)

                async with self._order_lock:
                    if order_id in self._pending_orders:
                        del self._pending_orders[order_id]

                logger.info(f"AlpacaBroker: Order {order_id} FILLED @ {order.filled_price}")

                # Memory leak fix: schedule cleanup after 1 hour (IBBroker pattern)
                def cleanup_order(oid: str = alpaca_order_id) -> None:
                    self._alpaca_order_map.pop(oid, None)

                loop = asyncio.get_event_loop()
                loop.call_later(3600, cleanup_order)

                # Sync positions after fill
                await self._sync_positions()

            elif event == "partial_fill":
                # Partial fill - update fill info but keep pending
                order.filled_quantity = (
                    float(order_data.filled_qty) if order_data.filled_qty else 0.0
                )
                logger.info(f"AlpacaBroker: Order {order_id} partial fill: {order.filled_quantity}")

            elif event in ("canceled", "expired", "rejected"):
                # Terminal state - update status and cleanup immediately
                if event == "rejected":
                    order.status = OrderStatus.REJECTED
                else:
                    order.status = OrderStatus.CANCELLED

                async with self._order_lock:
                    if order_id in self._pending_orders:
                        del self._pending_orders[order_id]

                # Memory leak fix: cleanup immediately (IBBroker pattern)
                self._alpaca_order_map.pop(alpaca_order_id, None)
                logger.info(f"AlpacaBroker: Order {order_id} {event.upper()}")

        except Exception as e:
            logger.error(f"AlpacaBroker: Error processing trade update: {e}")

    async def _run_trading_stream(self) -> None:
        """Run TradingStream in background thread.

        Note: TradingStream.run() calls asyncio.run() internally, so we must
        run it in a separate thread to avoid "cannot be called from a running
        event loop" errors.
        """
        try:
            logger.info("AlpacaBroker: Starting trading stream")
            if self._trading_stream:
                # Run in thread pool since .run() creates its own event loop
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._trading_stream.run)
        except asyncio.CancelledError:
            logger.info("AlpacaBroker: Trading stream cancelled")
            if self._trading_stream:
                self._trading_stream.stop()
        except Exception as e:
            logger.error(f"AlpacaBroker: Trading stream error: {e}")
            self._connected = False

    async def _sync_positions(self) -> None:
        """Sync positions from Alpaca via REST API."""
        if not self._trading_client:
            return

        try:
            alpaca_positions = self._trading_client.get_all_positions()

            async with self._position_lock:
                self._positions.clear()
                for pos in alpaca_positions:
                    symbol = pos.symbol.upper()
                    self._positions[symbol] = Position(
                        asset=symbol,
                        quantity=float(pos.qty),
                        entry_price=float(pos.avg_entry_price),
                        entry_time=datetime.now(timezone.utc),  # Alpaca doesn't provide entry time
                        current_price=float(pos.current_price) if pos.current_price else None,
                    )

            logger.info(f"AlpacaBroker: Synced {len(self._positions)} positions")

        except Exception as e:
            logger.error(f"AlpacaBroker: Failed to sync positions: {e}")

    async def _sync_orders(self) -> None:
        """Sync open orders from Alpaca."""
        if not self._trading_client:
            return

        try:
            request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
            alpaca_orders = self._trading_client.get_orders(request)

            async with self._order_lock:
                for alpaca_order in alpaca_orders:
                    # Generate order ID for existing order
                    self._order_counter += 1
                    order_id = f"ML4T-{self._order_counter}"

                    # Determine order type
                    order_type = OrderType.MARKET
                    if alpaca_order.limit_price:
                        if alpaca_order.stop_price:
                            order_type = OrderType.STOP_LIMIT
                        else:
                            order_type = OrderType.LIMIT
                    elif alpaca_order.stop_price:
                        order_type = OrderType.STOP

                    # Create our order object
                    self._pending_orders[order_id] = Order(
                        asset=alpaca_order.symbol.upper(),
                        side=OrderSide.BUY
                        if alpaca_order.side == AlpacaOrderSide.BUY
                        else OrderSide.SELL,
                        quantity=float(alpaca_order.qty) if alpaca_order.qty else 0.0,
                        order_type=order_type,
                        limit_price=float(alpaca_order.limit_price)
                        if alpaca_order.limit_price
                        else None,
                        stop_price=float(alpaca_order.stop_price)
                        if alpaca_order.stop_price
                        else None,
                        order_id=order_id,
                        status=self._map_order_status(alpaca_order.status),
                        created_at=alpaca_order.created_at or datetime.now(timezone.utc),
                    )
                    self._alpaca_order_map[str(alpaca_order.id)] = (order_id, time.time())

            logger.info(f"AlpacaBroker: Synced {len(self._pending_orders)} open orders")

        except Exception as e:
            logger.error(f"AlpacaBroker: Failed to sync orders: {e}")
