"""Protocol definitions for live trading.

This module defines the core protocols that enable broker-agnostic strategy execution.
All broker implementations must satisfy these protocols.

Key Design:
- BrokerProtocol: Synchronous interface for Strategy.on_data()
- AsyncBrokerProtocol: Async interface for broker implementations
- DataFeedProtocol: Real-time data feed interface

The sync/async split enables strategies to remain simple (no async/await) while
brokers can use modern async I/O for efficiency.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ml4t.backtest import IntentReconciliation
from ml4t.backtest.types import Order, OrderSide, OrderType, Position
from ml4t.specs import (
    CanonicalChildOrderIntent,
    CanonicalTargetIntent,
    ExecutionCapability,
    MarketEvent,
)

if TYPE_CHECKING:
    from ml4t.backtest.risk.position import PositionRule

LegacyFeedItem = tuple[datetime, dict[str, dict[str, Any]], dict[str, Any]]
FeedItem = MarketEvent | LegacyFeedItem


@runtime_checkable
class BrokerProtocol(Protocol):
    """Synchronous broker protocol for Strategy.on_data().

    This is the interface strategies interact with. It must be synchronous
    because Strategy.on_data() is synchronous (matches backtest behavior).

    ThreadSafeBrokerWrapper implements this protocol by wrapping an
    AsyncBrokerProtocol and using run_coroutine_threadsafe().

    Example:
        class MyStrategy(Strategy):
            def on_data(self, timestamp, data, context, broker: BrokerProtocol):
                # broker is sync - no async/await needed
                pos = broker.get_position("AAPL")
                if pos is None:
                    broker.submit_order("AAPL", 100)
    """

    @property
    def positions(self) -> dict[str, Position]:
        """Get all current positions.

        Returns:
            Dictionary mapping asset symbol to Position
        """
        ...

    @property
    def pending_orders(self) -> list[Order]:
        """Get all pending orders.

        Returns:
            List of pending Order objects
        """
        ...

    @property
    def is_connected(self) -> bool:
        """Check if broker is connected.

        Returns:
            True if connected and ready to trade
        """
        ...

    def get_position(self, asset: str) -> Position | None:
        """Get position for specific asset.

        Args:
            asset: Asset symbol (e.g., "AAPL")

        Returns:
            Position object if holding position, None otherwise
        """
        ...

    def get_positions(self) -> dict[str, Position]:
        """Get all current positions using the portable backtest-facing method."""
        ...

    def get_account_value(self) -> float:
        """Get total account value (cash + positions).

        Returns:
            Total account value in base currency
        """
        ...

    def get_cash(self) -> float:
        """Get available cash balance.

        Returns:
            Available cash in base currency
        """
        ...

    def get_pending_orders(self, asset: str | None = None) -> list[Order]:
        """Get pending orders, optionally filtered by asset."""
        ...

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
            quantity: Signed shares/contracts when side is omitted; positive unsigned
                shares/contracts when side is provided
            side: Order side (BUY/SELL), inferred from signed quantity if omitted
            order_type: Type of order (MARKET, LIMIT, STOP, etc.)
            limit_price: Limit price for LIMIT/STOP_LIMIT orders
            stop_price: Stop price for STOP/STOP_LIMIT orders
            **kwargs: Additional broker-specific parameters

        Returns:
            Order object with order_id and initial status

        Raises:
            ValueError: If order parameters are invalid
            RuntimeError: If broker is not connected
        """
        ...

    def cancel_order(self, order_id: str) -> bool:
        """Cancel pending order.

        Args:
            order_id: ID of order to cancel

        Returns:
            True if cancel request submitted, False if order not found
        """
        ...

    def replace_order(
        self,
        order_id: str,
        *,
        quantity: float | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> Order:
        """Replace a pending order with updated parameters."""
        ...

    def close_position(self, asset: str) -> Order | None:
        """Close entire position in asset.

        Convenience method that submits a closing order.

        Args:
            asset: Asset symbol to close

        Returns:
            Order object if position exists, None if no position
        """
        ...

    def register_target_intent(
        self,
        intent: CanonicalTargetIntent,
        *,
        position_rules: PositionRule | None = None,
    ) -> CanonicalTargetIntent:
        """Register an idempotent target for a causal opening phase."""
        ...

    def register_position_rule_policy(self, policy_id: str, rules: PositionRule) -> None:
        """Bind a portable policy identity to its client rule implementation."""
        ...

    def get_target_intents(self) -> tuple[CanonicalTargetIntent, ...]:
        """Return accepted canonical target intents."""
        ...

    def get_child_order_intents(self) -> tuple[CanonicalChildOrderIntent, ...]:
        """Return canonical child intents derived from accepted targets."""
        ...

    def get_intent_reconciliations(self) -> tuple[IntentReconciliation, ...]:
        """Return retained child fill and remainder evidence."""
        ...

    def export_target_intent_state(self) -> dict[str, Any]:
        """Serialize accepted target, child, reconciliation, and rule state."""
        ...

    def set_position_rules(
        self,
        rules: PositionRule | None,
        asset: str | None = None,
    ) -> None:
        """Set client-evaluated position rules globally or for one asset."""
        ...

    def clear_position_rules(self, asset: str | None = None) -> None:
        """Clear client-evaluated position rules globally or for one asset."""
        ...

    def update_position_context(self, asset: str, context: dict[str, Any]) -> None:
        """Update persistent context used by position-rule evaluation."""
        ...


@runtime_checkable
class AsyncBrokerProtocol(Protocol):
    """Asynchronous broker protocol for broker implementations.

    All broker implementations (IBBroker, AlpacaBroker, etc.) must implement
    this protocol. The async design enables efficient I/O without blocking.

    ThreadSafeBrokerWrapper wraps this protocol to provide BrokerProtocol
    for strategies.

    Example:
        class IBBroker:
            async def connect(self):
                await self.ib.connectAsync(...)

            async def submit_order_async(self, asset, quantity, ...):
                # Async I/O to IB
                trade = await self.ib.placeOrderAsync(...)
                return order
    """

    @property
    def positions(self) -> dict[str, Position]:
        """Return an independent snapshot of known positions."""
        ...

    @property
    def pending_orders(self) -> list[Order]:
        """Return an independent snapshot of known pending orders."""
        ...

    @property
    def is_connected(self) -> bool:
        """Return the cached provider connection state without blocking."""
        ...

    @property
    def execution_capabilities(self) -> frozenset[ExecutionCapability]:
        """Return the order capabilities implemented by the adapter."""
        ...

    async def connect(self) -> None:
        """Connect to broker and sync initial state.

        This should:
        1. Establish connection to broker API
        2. Sync current positions
        3. Sync pending orders
        4. Register event callbacks
        """
        ...

    async def disconnect(self) -> None:
        """Disconnect from broker gracefully."""
        ...

    async def is_connected_async(self) -> bool:
        """Check if broker is connected.

        Returns:
            True if connected and ready
        """
        ...

    def assert_paper_trading(self) -> None:
        """Fail unless the connected endpoint and account are paper-only."""
        ...

    def assert_live_trading(self) -> None:
        """Fail unless the connected endpoint and explicitly selected account are live."""
        ...

    async def get_positions_async(self) -> dict[str, Position]:
        """Get all positions (async version)."""
        ...

    async def get_pending_orders_async(self, asset: str | None = None) -> list[Order]:
        """Get pending orders, optionally filtered by asset."""
        ...

    async def get_position_async(self, asset: str) -> Position | None:
        """Get position for asset (async version)."""
        ...

    async def get_account_value_async(self) -> float:
        """Get total account value (async version)."""
        ...

    async def get_cash_async(self) -> float:
        """Get available cash (async version)."""
        ...

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
        """Submit an order using the same quantity contract as ``submit_order``."""
        ...

    async def cancel_order_async(self, order_id: str) -> bool:
        """Cancel order (async version)."""
        ...

    async def replace_order_async(
        self,
        order_id: str,
        *,
        quantity: float | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> Order:
        """Replace a pending order with updated parameters (async version)."""
        ...

    async def close_position_async(self, asset: str) -> Order | None:
        """Close position (async version)."""
        ...


@runtime_checkable
class DataFeedProtocol(Protocol):
    """Protocol for real-time data feeds.

    Stable-supported feeds provide an async iterator of validated ``MarketEvent`` objects.
    The legacy tuple member of ``FeedItem`` is temporary compatibility for experimental feeds.
    The feed handles:
    - Subscribing to market data
    - Aggregating ticks to bars (if needed)
    - Emitting data on schedule (e.g., every minute)

    Example:
        class IBDataFeed:
            async def start(self):
                await self._subscribe_to_ticks()

            async def __aiter__(self):
                return self

            async def __anext__(self):
                return await self._event_queue.get()

            def stop(self):
                self._running = False
    """

    async def start(self) -> None:
        """Start the data feed.

        This should:
        1. Subscribe to market data
        2. Start internal aggregation/buffering
        3. Begin emitting data
        """
        ...

    def stop(self) -> None:
        """Stop the data feed gracefully.

        Should be non-blocking. The feed should stop after
        the current iteration completes.
        """
        ...

    def __aiter__(
        self,
    ) -> AsyncIterator[FeedItem]:
        """Return async iterator.

        Yields:
            A validated market event, or a temporary legacy tuple from an experimental feed.
        """
        ...

    async def __anext__(self) -> FeedItem:
        """Get next market event.

        Returns:
            A validated market event, or a temporary legacy tuple from an experimental feed.

        Raises:
            StopAsyncIteration: When feed ends
        """
        ...
