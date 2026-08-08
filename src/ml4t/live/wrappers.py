"""Thread-safe wrappers for async brokers.

This module provides the ThreadSafeBrokerWrapper that bridges the sync/async
boundary between Strategy.on_data() and async broker implementations.

Key Design:
- Every portable strategy callback runs on one dedicated worker thread
- Broker methods run on main event loop
- run_coroutine_threadsafe() handles cross-thread communication
- Differentiated timeouts (5s getters, 30s orders)
"""

import asyncio
import logging
import threading
from typing import Any

from ml4t.backtest.types import Order, OrderSide, OrderType, Position

from ml4t.live.protocols import AsyncBrokerProtocol

logger = logging.getLogger(__name__)


class ThreadSafeBrokerWrapper:
    """Wraps an async broker for use from sync strategy code.

    This wrapper is passed to Strategy.on_data() instead of the raw broker.
    It bridges the sync/async boundary by scheduling coroutines on the main
    event loop and blocking the worker thread until they complete.

    Thread Safety:
    - Every portable strategy callback runs on one dedicated worker thread
    - Broker methods run on main event loop
    - run_coroutine_threadsafe() handles the cross-thread communication

    Timeouts (from design review):
    - Getters (get_cash, get_account_value): 5s
    - Order operations (submit, cancel, close): 30s

    Example:
        # LiveEngine creates this wrapper
        loop = asyncio.get_running_loop()
        wrapped = ThreadSafeBrokerWrapper(ib_broker, loop)

        # Strategy uses it like a normal sync broker
        order = wrapped.submit_order('AAPL', 100, OrderSide.BUY)

    Note:
        This class implements BrokerProtocol but does not inherit from it.
        It provides a sync interface backed by async operations.
    """

    def __init__(self, async_broker: AsyncBrokerProtocol, loop: asyncio.AbstractEventLoop):
        """Initialize thread-safe wrapper.

        Args:
            async_broker: Async broker implementation (IBBroker, etc.)
            loop: Main event loop (from asyncio.get_running_loop())
        """
        self._broker = async_broker
        self._loop = loop
        self._loop_thread_id = threading.get_ident()

    # === Properties (direct access, assumed thread-safe) ===

    @property
    def positions(self) -> dict[str, Position]:
        """Get current positions (thread-safe read).

        Returns:
            Dictionary mapping asset symbol to Position
        """
        # IBBroker.positions property returns a copy, which is thread-safe
        return self._broker.positions

    @property
    def pending_orders(self) -> list[Order]:
        """Get pending orders (thread-safe read).

        Returns:
            List of pending Order objects
        """
        # IBBroker.pending_orders property returns a copy, which is thread-safe
        return self._broker.pending_orders

    @property
    def is_connected(self) -> bool:
        """Check if broker is connected.

        Returns:
            True if connected and ready to trade
        """
        # Simple boolean read is thread-safe
        return self._run_sync(self._broker.is_connected_async(), timeout=5.0)

    # === Sync methods that wrap async operations ===

    def get_position(self, asset: str) -> Position | None:
        """Get position for specific asset.

        Args:
            asset: Asset symbol (e.g., "AAPL")

        Returns:
            Position object if holding position, None otherwise

        Raises:
            TimeoutError: If operation times out
            RuntimeError: If broker error occurs
        """
        # Can use positions property since it returns a copy
        return self.positions.get(asset)

    def get_account_value(self) -> float:
        """Get total account value (cash + positions).

        Returns:
            Total account value in base currency

        Raises:
            TimeoutError: If operation times out (5s)
            RuntimeError: If broker error occurs
        """
        return self._run_sync(self._broker.get_account_value_async(), timeout=5.0)

    def get_cash(self) -> float:
        """Get available cash balance.

        Returns:
            Available cash in base currency

        Raises:
            TimeoutError: If operation times out (5s)
            RuntimeError: If broker error occurs
        """
        return self._run_sync(self._broker.get_cash_async(), timeout=5.0)

    def get_pending_orders(self, asset: str | None = None) -> list[Order]:
        """Get pending orders, optionally filtered by asset."""
        operation = (
            self._broker.get_pending_orders_async()
            if asset is None
            else self._broker.get_pending_orders_async(asset)
        )
        return self._run_sync(operation, timeout=5.0)

    def submit_order(
        self,
        asset: str,
        quantity: float,
        side: OrderSide | None = None,
        order_type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
        stop_price: float | None = None,
        **kwargs: Any,
    ) -> Order:
        """Submit order for execution.

        Args:
            asset: Asset symbol (e.g., "AAPL")
            quantity: Number of shares/contracts (positive for buy, negative for sell)
            side: Order side (BUY/SELL), auto-detected from quantity if None
            order_type: Type of order (MARKET, LIMIT, STOP, etc.)
            limit_price: Limit price for LIMIT/STOP_LIMIT orders
            stop_price: Stop price for STOP/STOP_LIMIT orders
            **kwargs: Additional broker-specific parameters

        Returns:
            Order object with order_id and initial status

        Raises:
            TimeoutError: If operation times out (30s)
            ValueError: If order parameters are invalid
            RuntimeError: If broker is not connected or error occurs
        """
        return self._run_sync(
            self._broker.submit_order_async(
                asset, quantity, side, order_type, limit_price, stop_price, **kwargs
            ),
            timeout=30.0,  # Orders need longer timeout
        )

    def cancel_order(self, order_id: str) -> bool:
        """Cancel pending order.

        Args:
            order_id: ID of order to cancel

        Returns:
            True if cancel request submitted, False if order not found

        Raises:
            TimeoutError: If operation times out (30s)
            RuntimeError: If broker error occurs
        """
        return self._run_sync(self._broker.cancel_order_async(order_id), timeout=30.0)

    def replace_order(
        self,
        order_id: str,
        *,
        quantity: float | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> Order:
        """Replace a pending order with updated parameters."""
        return self._run_sync(
            self._broker.replace_order_async(
                order_id,
                quantity=quantity,
                limit_price=limit_price,
                stop_price=stop_price,
            ),
            timeout=30.0,
        )

    def close_position(self, asset: str) -> Order | None:
        """Close entire position in asset.

        Convenience method that submits a closing order.

        Args:
            asset: Asset symbol to close

        Returns:
            Order object if position exists, None if no position

        Raises:
            TimeoutError: If operation times out (30s)
            RuntimeError: If broker error occurs
        """
        return self._run_sync(self._broker.close_position_async(asset), timeout=30.0)

    def _run_sync(self, coro: Any, timeout: float = 5.0) -> Any:
        """Schedule coroutine on main loop and wait for result.

        This blocks the worker thread but NOT the main event loop.

        Args:
            coro: Coroutine to run
            timeout: Timeout in seconds (default: 5.0)

        Returns:
            Result of the coroutine

        Raises:
            TimeoutError: If operation times out
            RuntimeError: If the event loop is closed or other error occurs

        Timeouts (from design review):
        - Getters (get_cash, get_account_value): 5s (default)
        - Order operations (submit, cancel, close): 30s
        """
        if threading.get_ident() == self._loop_thread_id:
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            raise RuntimeError(
                "Synchronous broker methods require a strategy worker thread; "
                "await the async broker method from the event-loop thread"
            )
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            logger.error(f"ThreadSafeBrokerWrapper: Operation timed out after {timeout}s")
            raise
        except Exception as e:
            logger.error(f"ThreadSafeBrokerWrapper: Error running coroutine: {e}", exc_info=True)
            raise RuntimeError(f"Broker operation failed: {e}") from e
