"""Safety and risk management components for live trading.

This module provides risk controls, position tracking, and state persistence:
- LiveRiskConfig: Configuration dataclass for risk limits
- RiskState: Persisted state that survives restarts
- VirtualPortfolio: Shadow position tracking for paper trading
- SafeBroker: Risk-controlled broker wrapper

The design addresses several critical safety issues identified in code review:
- Shadow mode with realistic position tracking (prevents infinite buy loops)
- State persistence across crashes (atomic JSON writes)
- Memory leak prevention (_prune_history)
- Multiple layers of risk controls
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from ml4t.backtest.types import Order, OrderSide, OrderStatus, OrderType, Position

from .protocols import AsyncBrokerProtocol

logger = logging.getLogger(__name__)


@dataclass
class LiveRiskConfig:
    """Risk configuration for live trading.

    Multiple layers of protection - all limits are optional.
    Set to inf/large values to disable specific checks.

    Example:
        # Conservative configuration
        config = LiveRiskConfig(
            max_position_value=25_000.0,
            max_daily_loss=2_000.0,
            shadow_mode=True,  # Always start with shadow mode!
        )

        # Disable specific checks
        config = LiveRiskConfig(
            max_position_value=float('inf'),  # No position limit
            max_daily_loss=10_000.0,          # Only daily loss limit
        )

    Safety Recommendations:
        1. Always start with shadow_mode=True
        2. Graduate to paper trading
        3. Use small positions when going live
        4. Set conservative risk limits
    """

    # Position limits
    max_position_value: float = 50_000.0  # Max $ per position
    max_position_shares: int = 1000  # Max shares per position
    max_total_exposure: float = 200_000.0  # Max total $ across all positions
    max_positions: int = 20  # Max number of positions

    # Order limits
    max_order_value: float = 10_000.0  # Max $ per order
    max_order_shares: int = 500  # Max shares per order
    max_orders_per_minute: int = 10  # Rate limit

    # Loss limits
    max_daily_loss: float = 5_000.0  # Stop if exceeded
    max_drawdown_pct: float = 0.05  # Stop if 5% drawdown

    # Price protection
    max_price_deviation_pct: float = 0.05  # Fat finger: reject if limit >5% from market
    max_data_staleness_seconds: float = 60.0  # Reject if data older than 60s
    dedup_window_seconds: float = 1.0  # Block duplicate orders within 1s

    # Asset restrictions
    allowed_assets: set[str] = field(default_factory=set)
    blocked_assets: set[str] = field(default_factory=set)

    # Shadow mode - log orders but don't execute
    shadow_mode: bool = False

    # Kill switch
    kill_switch_enabled: bool = False
    allow_reducing_risk_when_killed: bool = True
    halt_on_reducing_risk_failure: bool = True

    # Startup reconciliation
    fail_on_reconciliation_mismatch: bool = False

    # State persistence
    state_file: str = ".ml4t_risk_state.json"
    journal_file: str | None = None

    def __post_init__(self):
        """Validate configuration parameters."""
        # Validate position limits
        if self.max_position_value <= 0:
            raise ValueError(f"max_position_value must be positive, got {self.max_position_value}")

        if self.max_position_shares <= 0:
            raise ValueError(
                f"max_position_shares must be positive, got {self.max_position_shares}"
            )

        if self.max_total_exposure <= 0:
            raise ValueError(f"max_total_exposure must be positive, got {self.max_total_exposure}")

        if self.max_positions <= 0:
            raise ValueError(f"max_positions must be positive, got {self.max_positions}")

        # Validate order limits
        if self.max_order_value <= 0:
            raise ValueError(f"max_order_value must be positive, got {self.max_order_value}")

        if self.max_order_shares <= 0:
            raise ValueError(f"max_order_shares must be positive, got {self.max_order_shares}")

        if self.max_orders_per_minute <= 0:
            raise ValueError(
                f"max_orders_per_minute must be positive, got {self.max_orders_per_minute}"
            )

        # Validate loss limits
        if self.max_daily_loss <= 0:
            raise ValueError(f"max_daily_loss must be positive, got {self.max_daily_loss}")

        if not 0 < self.max_drawdown_pct <= 1:
            raise ValueError(
                f"max_drawdown_pct must be between 0 and 1, got {self.max_drawdown_pct}"
            )

        # Validate price protection
        if not 0 < self.max_price_deviation_pct <= 1:
            raise ValueError(
                f"max_price_deviation_pct must be between 0 and 1, "
                f"got {self.max_price_deviation_pct}"
            )

        if self.max_data_staleness_seconds <= 0:
            raise ValueError(
                f"max_data_staleness_seconds must be positive, "
                f"got {self.max_data_staleness_seconds}"
            )

        if self.dedup_window_seconds < 0:
            raise ValueError(
                f"dedup_window_seconds must be non-negative, got {self.dedup_window_seconds}"
            )

        # Validate asset restrictions
        if self.allowed_assets and self.blocked_assets:
            overlap = self.allowed_assets & self.blocked_assets
            if overlap:
                raise ValueError(f"Assets cannot be in both allowed and blocked lists: {overlap}")

        # Validate state file path
        if not self.state_file:
            raise ValueError("state_file cannot be empty")
        if self.journal_file is not None and not self.journal_file:
            raise ValueError("journal_file cannot be empty when provided")


@dataclass
class RiskState:
    """Persisted risk state - survives restarts.

    This state is saved to disk after every order and on shutdown.
    Uses atomic JSON writes (write to .tmp then os.replace) to prevent corruption.

    Example:
        state = RiskState(date="2023-10-15", daily_loss=1500.0)
        state.kill_switch_activated = True
        state.kill_switch_reason = "Max daily loss exceeded"

    Note:
        The kill switch state persists across restarts and must be manually reset
        by deleting the state file or setting kill_switch_activated=False.
    """

    date: str  # YYYY-MM-DD
    daily_loss: float = 0.0  # Cumulative daily loss
    orders_placed: int = 0  # Orders placed today
    high_water_mark: float = 0.0  # Session high equity
    session_start_equity: float | None = None  # Baseline for daily loss checks
    persisted_positions: dict[str, float] = field(default_factory=dict)
    persisted_pending_orders: list[dict[str, Any]] = field(default_factory=list)
    portable_strategy_state: dict[str, Any] = field(default_factory=dict)
    shadow_portfolio: dict[str, Any] = field(default_factory=dict)
    kill_switch_activated: bool = False  # Was kill switch triggered?
    kill_switch_reason: str = ""  # Why?

    @classmethod
    def from_dict(cls, data: dict) -> "RiskState":
        """Create RiskState from dictionary (for JSON loading).

        Args:
            data: Dictionary with RiskState fields

        Returns:
            RiskState instance
        """
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary (for JSON saving).

        Returns:
            Dictionary with all fields
        """
        data = {
            "date": self.date,
            "daily_loss": self.daily_loss,
            "orders_placed": self.orders_placed,
            "high_water_mark": self.high_water_mark,
            "kill_switch_activated": self.kill_switch_activated,
            "kill_switch_reason": self.kill_switch_reason,
        }
        if self.session_start_equity is not None:
            data["session_start_equity"] = self.session_start_equity
        if self.persisted_positions:
            data["persisted_positions"] = self.persisted_positions
        if self.persisted_pending_orders:
            data["persisted_pending_orders"] = self.persisted_pending_orders
        if self.portable_strategy_state:
            data["portable_strategy_state"] = self.portable_strategy_state
        if self.shadow_portfolio:
            data["shadow_portfolio"] = self.shadow_portfolio
        return data

    @staticmethod
    def save_atomic(state: "RiskState", filepath: str) -> None:
        """Save state with atomic write (write to .tmp then os.replace).

        This prevents corruption if process dies mid-write.

        Args:
            state: RiskState to save
            filepath: Path to save to

        Raises:
            OSError: If write fails
        """
        tmp_file = f"{filepath}.tmp"
        with open(tmp_file, "w") as f:
            json.dump(state.to_dict(), f, indent=2)

        # Atomic rename (POSIX guarantees atomicity)
        os.replace(tmp_file, filepath)

    @staticmethod
    def load(filepath: str) -> "RiskState | None":
        """Load state from file.

        Args:
            filepath: Path to load from

        Returns:
            RiskState if file exists and is valid, None otherwise
        """
        path = Path(filepath)
        if not path.exists():
            return None

        try:
            with open(filepath) as f:
                data = json.load(f)
            return RiskState.from_dict(data)
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            # Corrupted file or invalid format
            print(f"Warning: Could not load risk state from {filepath}: {e}")
            return None

    @staticmethod
    def create_for_today() -> "RiskState":
        """Create new state for today's date.

        Returns:
            RiskState with today's date and default values
        """
        return RiskState(date=datetime.now().strftime("%Y-%m-%d"))


@dataclass
class MarketSnapshot:
    """Latest known market reference for an asset."""

    timestamp: datetime
    observed_at: datetime
    price: float


class VirtualPortfolio:
    """Manages internal accounting for Shadow Mode (Paper Trading).

    Addresses Gemini's Critical Issue A: "The Infinite Buy Loop"

    Problem: In shadow mode, returning fake Order objects without updating
    position state causes strategies to keep buying forever because
    get_position() always returns None.

    Solution: Track shadow positions locally. When shadow_mode=True:
    - submit_order() updates this virtual portfolio
    - positions/get_position() return from this portfolio
    - Strategy sees realistic position state

    Handles:
    - New positions
    - Position increases (weighted avg cost basis)
    - Position decreases (partial close)
    - Position close (quantity = 0)
    - Position flip (long -> short or vice versa)

    Example:
        portfolio = VirtualPortfolio(initial_cash=100_000.0)

        # Simulate buy order fill
        order = Order(
            asset="AAPL",
            side=OrderSide.BUY,
            quantity=100,
            filled_price=150.0,
            filled_quantity=100,
            ...
        )
        portfolio.process_fill(order)

        # Check position
        pos = portfolio.positions.get("AAPL")
        assert pos.quantity == 100
        assert pos.entry_price == 150.0

        # Simulate sell order fill (close)
        sell_order = Order(
            asset="AAPL",
            side=OrderSide.SELL,
            quantity=100,
            filled_price=155.0,
            filled_quantity=100,
            ...
        )
        portfolio.process_fill(sell_order)
        assert "AAPL" not in portfolio.positions
    """

    def __init__(self, initial_cash: float = 100_000.0):
        """Initialize virtual portfolio.

        Args:
            initial_cash: Starting cash balance (default: 100,000)
        """
        self._initial_cash = initial_cash
        self._cash = initial_cash
        self._positions: dict[str, Position] = {}

    @property
    def positions(self) -> dict[str, Position]:
        """Get current positions (returns copy for safety).

        Returns:
            Dictionary mapping asset symbol to Position
        """
        return dict(self._positions.items())

    @property
    def cash(self) -> float:
        """Get current cash balance.

        Returns:
            Available cash
        """
        return self._cash

    @property
    def account_value(self) -> float:
        """Get total account value (cash + position market value).

        Returns:
            Total account value
        """
        market_value = sum(
            abs(p.quantity) * (p.current_price or p.entry_price) for p in self._positions.values()
        )
        return self._cash + market_value

    def process_fill(self, order: Order) -> None:
        """Update state based on filled shadow order.

        Handles:
        - Weighted average cost basis for position increases
        - Position flipping (long -> short or vice versa)
        - Partial and full closes

        Args:
            order: Filled Order object (must have filled_quantity and filled_price)
        """
        if not order.filled_quantity or not order.filled_price:
            logger.warning(f"VirtualPortfolio: Order {order.order_id} has no fill info")
            return

        asset = order.asset
        fill_qty = order.filled_quantity
        fill_price = order.filled_price
        transaction_value = fill_qty * fill_price

        # Cash impact
        if order.side == OrderSide.BUY:
            self._cash -= transaction_value
            signed_qty = fill_qty
        else:
            self._cash += transaction_value
            signed_qty = -fill_qty

        current = self._positions.get(asset)

        if current is None:
            # New position
            self._positions[asset] = Position(
                asset=asset,
                quantity=signed_qty,
                entry_price=fill_price,
                entry_time=datetime.now(),
                current_price=fill_price,
            )
            logger.info(
                f"Shadow: Opened {asset} {'LONG' if signed_qty > 0 else 'SHORT'} {abs(signed_qty)}"
            )

        else:
            old_qty = current.quantity
            new_qty = old_qty + signed_qty

            if new_qty == 0:
                # Position closed
                del self._positions[asset]
                logger.info(f"Shadow: Closed {asset}")

            elif (old_qty > 0 and new_qty < 0) or (old_qty < 0 and new_qty > 0):
                # Position flipped (e.g., Long 100 -> Sell 200 -> Short 100)
                self._positions[asset] = Position(
                    asset=asset,
                    quantity=new_qty,
                    entry_price=fill_price,  # Reset basis on flip
                    entry_time=datetime.now(),
                    current_price=fill_price,
                )
                logger.info(f"Shadow: Flipped {asset} to {new_qty}")

            elif abs(new_qty) > abs(old_qty):
                # Increasing position - weighted average cost basis
                total_old = old_qty * current.entry_price
                total_new = signed_qty * fill_price
                new_avg = (total_old + total_new) / new_qty
                current.quantity = new_qty
                current.entry_price = abs(new_avg)
                current.current_price = fill_price
                logger.info(
                    f"Shadow: Increased {asset} to {new_qty}, basis ${current.entry_price:.2f}"
                )

            else:
                # Decreasing position (partial close) - basis unchanged
                current.quantity = new_qty
                current.current_price = fill_price
                logger.info(f"Shadow: Reduced {asset} to {new_qty}")

    def update_prices(self, prices: dict[str, float]) -> None:
        """Update current prices for accurate account value.

        Args:
            prices: Dictionary mapping asset symbol to current price
        """
        for asset, price in prices.items():
            if asset in self._positions:
                self._positions[asset].current_price = price

    def to_state(self) -> dict[str, Any]:
        """Return restart-safe shadow cash and position state."""
        return {
            "cash": self._cash,
            "positions": [
                {
                    "asset": position.asset,
                    "quantity": position.quantity,
                    "entry_price": position.entry_price,
                    "entry_time": position.entry_time.isoformat(),
                    "current_price": position.current_price,
                    "multiplier": position.multiplier,
                    "context": position.context,
                }
                for position in self._positions.values()
            ],
        }

    def restore_state(self, state: dict[str, Any]) -> None:
        """Restore shadow state before runtime reconciliation."""
        self._cash = float(state.get("cash", self._cash))
        self._positions.clear()
        for raw in state.get("positions", ()):
            entry_time = datetime.fromisoformat(raw["entry_time"])
            self._positions[raw["asset"]] = Position(
                asset=raw["asset"],
                quantity=float(raw["quantity"]),
                entry_price=float(raw["entry_price"]),
                entry_time=entry_time,
                current_price=float(raw["current_price"]),
                multiplier=float(raw.get("multiplier", 1.0)),
                context=dict(raw.get("context", {})),
            )


class RiskLimitError(Exception):
    """Raised when an order violates risk limits."""

    pass


class ReconciliationMismatchError(RuntimeError):
    """Raised when startup reconciliation is configured to fail closed."""

    pass


class SafeBroker:
    """Risk-controlled wrapper with state persistence.

    Addresses Gemini v1: "If script crashes and restarts, SafeBroker resets
    max_daily_loss to 0. A losing strategy could burn through the limit again."

    Addresses Gemini v2:
    - Critical Issue A: VirtualPortfolio for shadow mode
    - Memory leaks: _recent_orders pruned even if dedup disabled
    - Atomic JSON writes: write to .tmp then os.replace()

    Safety Features:
    1. Pre-trade validation against all risk limits
    2. Order rate limiting
    3. Drawdown monitoring with kill switch
    4. Fat finger protection (price deviation check)
    5. Stale data protection
    6. Duplicate order filter
    7. Shadow mode with VirtualPortfolio (realistic paper trading)
    8. State persistence across restarts

    Example:
        broker = IBBroker()
        await broker.connect()

        safe = SafeBroker(
            broker=broker,
            config=LiveRiskConfig(
                max_position_value=25000,
                shadow_mode=True,  # Test first!
            )
        )

        # Use safe in strategy
        engine = LiveEngine(strategy, safe, feed)
    """

    def __init__(self, broker: AsyncBrokerProtocol, config: LiveRiskConfig):
        """Initialize SafeBroker.

        Args:
            broker: Async broker implementation (IBBroker, AlpacaBroker, etc.)
            config: Risk configuration
        """
        self._broker = broker
        self.config = config

        # Load or initialize state
        self._state = self._load_state()

        # Rate limiting
        self._order_timestamps: list[float] = []

        # Duplicate detection
        self._recent_orders: list[tuple[float, str, float]] = []  # (time, asset, qty)

        # Latest market reference per asset, populated by LiveEngine
        self._latest_market_data: dict[str, MarketSnapshot] = {}
        self._last_reconciliation_report: dict[str, Any] | None = None

        # NEW: VirtualPortfolio for shadow mode (Gemini v2 fix)
        self._virtual_portfolio = VirtualPortfolio(initial_cash=100_000.0)
        if config.shadow_mode and self._state.shadow_portfolio:
            self._virtual_portfolio.restore_state(self._state.shadow_portfolio)

        # Initialize high water mark if not set
        if self._state.high_water_mark == 0.0:
            try:
                # Note: This is sync access to async method - we'll fix this
                # by making initialization async if needed
                pass
            except Exception:
                pass

        logger.info(f"SafeBroker initialized. Shadow mode: {config.shadow_mode}")
        if self._state.kill_switch_activated:
            logger.warning(
                f"Kill switch was previously activated: {self._state.kill_switch_reason}"
            )

    # === AsyncBrokerProtocol Implementation ===
    # NEW: Routes to VirtualPortfolio when shadow_mode=True (Gemini v2 fix)

    @property
    def positions(self) -> dict[str, Position]:
        """Get current positions.

        In shadow mode, returns virtual positions.
        In live mode, returns broker positions.
        """
        if self.config.shadow_mode:
            return self._virtual_portfolio.positions
        return self._broker.positions  # type: ignore[attr-defined]

    @property
    def pending_orders(self) -> list[Order]:
        """Get pending orders."""
        return self._broker.pending_orders  # type: ignore[attr-defined]

    @property
    def reconciliation_report(self) -> dict[str, Any] | None:
        """Return the latest startup reconciliation report."""
        return self._last_reconciliation_report

    @property
    def is_connected(self) -> bool:
        """Check if broker is connected."""
        # Simplified check - actual implementation might need async
        try:
            return bool(self._broker._connected) if hasattr(self._broker, "_connected") else True
        except Exception:
            return True

    @property
    def execution_capabilities(self) -> frozenset[Any]:
        """Return capabilities declared by the wrapped venue."""
        return frozenset(getattr(self._broker, "execution_capabilities", ()))

    def load_portable_strategy_state(self) -> dict[str, Any]:
        """Return a copy of persisted target and position-rule state."""
        return json.loads(json.dumps(self._state.portable_strategy_state))

    def save_portable_strategy_state(self, state: dict[str, Any]) -> None:
        """Persist target and position-rule state with the safety state."""
        self._state.portable_strategy_state = json.loads(json.dumps(state))
        self._save_state()

    def get_position(self, asset: str) -> Position | None:
        """Get position for specific asset.

        Args:
            asset: Asset symbol

        Returns:
            Position object or None
        """
        if self.config.shadow_mode:
            return self._virtual_portfolio.positions.get(asset)
        return self._broker.get_position(asset)  # type: ignore[attr-defined]

    async def get_account_value_async(self) -> float:
        """Get total account value (async).

        Returns:
            Total account value in base currency
        """
        if self.config.shadow_mode:
            return self._virtual_portfolio.account_value
        return await self._broker.get_account_value_async()

    async def get_cash_async(self) -> float:
        """Get available cash (async).

        Returns:
            Available cash in base currency
        """
        if self.config.shadow_mode:
            return self._virtual_portfolio.cash
        return await self._broker.get_cash_async()

    async def cancel_order_async(self, order_id: str) -> bool:
        """Cancel pending order.

        Args:
            order_id: ID of order to cancel

        Returns:
            True if cancel request submitted
        """
        cancelled = await self._broker.cancel_order_async(order_id)
        self.record_event(
            "order_cancel_requested",
            order_id=order_id,
            cancelled=cancelled,
        )
        return cancelled

    async def close_position_async(self, asset: str) -> Order | None:
        """Close entire position.

        Close positions bypass normal limits (safety feature).

        Args:
            asset: Asset symbol to close

        Returns:
            Order object if position exists
        """
        position = self.get_position(asset)
        if position is None or position.quantity == 0:
            return None
        return await self.reduce_position_async(
            asset,
            abs(position.quantity),
            reason="close_position",
            idempotency_key=(
                f"close:{asset}:"
                f"{position.entry_time.isoformat() if position.entry_time else 'unknown-entry'}"
            ),
        )

    async def reduce_position_async(
        self,
        asset: str,
        quantity: float,
        *,
        reason: str,
        idempotency_key: str,
        fill_price: float | None = None,
    ) -> Order:
        """Submit an explicitly reducing order under the configured kill-switch policy."""
        position = self.get_position(asset)
        if position is None or quantity <= 0 or quantity > abs(position.quantity):
            raise RiskLimitError(f"Reducing order for {asset} exceeds the open position")
        if (
            self.config.kill_switch_enabled or self._state.kill_switch_activated
        ) and not self.config.allow_reducing_risk_when_killed:
            raise RiskLimitError("Kill switch policy blocks reducing-risk orders")
        side = OrderSide.SELL if position.quantity > 0 else OrderSide.BUY
        price = fill_price
        if price is None and self.config.shadow_mode:
            snapshot = self._get_market_snapshot(asset)
            price = (
                snapshot.price
                if snapshot is not None
                else float(position.current_price or position.entry_price)
            )
        try:
            if self.config.shadow_mode:
                order = Order(
                    asset=asset,
                    side=side,
                    quantity=quantity,
                    order_type=OrderType.MARKET,
                    order_id=f"SHADOW-REDUCE-{int(time.time() * 1_000_000)}",
                    status=OrderStatus.FILLED,
                    filled_quantity=quantity,
                    filled_price=price,
                    filled_at=datetime.now(UTC),
                    intent_idempotency_key=idempotency_key,
                )
                self._virtual_portfolio.process_fill(order)
            else:
                if reason == "close_position" and quantity == abs(position.quantity):
                    order = await self._broker.close_position_async(asset)
                    if order is None:
                        raise RiskLimitError(f"Venue did not create a closing order for {asset}")
                else:
                    order = await self._broker.submit_order_async(
                        asset,
                        quantity,
                        side,
                        OrderType.MARKET,
                        reducing_risk=True,
                        reason=reason,
                        intent_idempotency_key=idempotency_key,
                    )
        except Exception as error:
            self.record_event(
                "reducing_risk_failed",
                asset=asset,
                quantity=quantity,
                reason=reason,
                idempotency_key=idempotency_key,
                error_type=type(error).__name__,
                error=str(error),
            )
            if self.config.halt_on_reducing_risk_failure:
                raise
            raise RiskLimitError(f"Reducing-risk order failed for {asset}: {error}") from error
        self._state.orders_placed += 1
        self._refresh_state_snapshot_from_cache()
        self._save_state()
        self.record_event(
            "reducing_risk_submitted",
            asset=asset,
            order_id=self._order_identifier(order),
            quantity=quantity,
            reason=reason,
            idempotency_key=idempotency_key,
        )
        return order

    async def replace_order_async(
        self,
        order_id: str,
        *,
        quantity: float | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> Order:
        """Replace a pending order via cancel-and-resubmit."""
        original = self._pending_order_by_id(order_id)
        if original is None:
            raise RiskLimitError(f"Pending order {order_id} not found")

        replacement_quantity = float(original.quantity if quantity is None else quantity)
        replacement_limit = original.limit_price if limit_price is None else limit_price
        replacement_stop = original.stop_price if stop_price is None else stop_price

        asset = original.asset.upper()
        self._recent_orders = [
            entry
            for entry in self._recent_orders
            if not (entry[1] == asset and abs(entry[2] - float(original.quantity)) < 0.01)
        ]

        cancelled = await self.cancel_order_async(order_id)
        if not cancelled:
            raise RiskLimitError(f"Could not cancel order {order_id} before replacement")

        replacement = await self.submit_order_async(
            asset=original.asset,
            quantity=int(replacement_quantity),
            side=original.side,
            order_type=original.order_type,
            limit_price=replacement_limit,
            stop_price=replacement_stop,
        )
        self.record_event(
            "order_replaced",
            original_order_id=order_id,
            replacement_order_id=self._order_identifier(replacement),
            asset=original.asset,
            quantity=replacement_quantity,
            order_type=original.order_type.value,
        )
        return replacement

    # === Risk-Controlled Order Submission ===

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
        """Submit order with full risk validation.

        Args:
            asset: Asset symbol
            quantity: Number of shares/contracts
            side: Order side (BUY/SELL), auto-detected from quantity if None
            order_type: Type of order
            limit_price: Limit price for LIMIT/STOP_LIMIT orders
            stop_price: Stop price for STOP/STOP_LIMIT orders
            **kwargs: Additional broker-specific parameters

        Returns:
            Order object

        Raises:
            RiskLimitError: If order violates any risk limit
        """
        # Infer side
        if side is None:
            side = OrderSide.BUY if quantity > 0 else OrderSide.SELL
            quantity = abs(quantity)

        # === Risk Checks ===

        # 1. Kill switch
        if self.config.kill_switch_enabled or self._state.kill_switch_activated:
            raise RiskLimitError(
                f"Kill switch active: {self._state.kill_switch_reason or 'Manual activation'}"
            )

        # 2. Asset check
        self._check_asset(asset)

        # 3. Fresh market data required for live risk checks
        self._check_data_staleness(asset)

        # 4. Daily loss check
        await self._check_daily_loss()

        # 5. Duplicate check
        self._check_duplicate(asset, float(quantity))

        # 6. Rate limit
        self._check_rate_limit()

        # 7. Order size limits
        price = await self._estimate_price(asset, limit_price)
        order_value = abs(quantity) * price
        self._check_order_limits(quantity, order_value)

        # 8. Position limits
        await self._check_position_limits(asset, quantity, order_value, side)

        # 9. Fat finger check (limit orders)
        if limit_price and order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT):
            await self._check_price_deviation(asset, limit_price)

        # 10. Drawdown check (may activate kill switch)
        await self._check_drawdown()

        # === Shadow Mode (Gemini v2 fix: use VirtualPortfolio) ===
        if self.config.shadow_mode:
            # Create filled order
            order = Order(
                asset=asset,
                side=side,
                quantity=quantity,
                order_type=order_type,
                limit_price=limit_price,
                stop_price=stop_price,
                order_id=f"SHADOW-{int(time.time() * 1000)}",
                status=OrderStatus.FILLED,
                filled_quantity=quantity,
                filled_price=price,
                filled_at=datetime.now(UTC),
                target_intent_id=kwargs.get("target_intent_id"),
                child_intent_id=kwargs.get("child_intent_id"),
                intent_idempotency_key=kwargs.get("intent_idempotency_key"),
            )

            # CRITICAL: Update VirtualPortfolio (fixes infinite buy loop)
            self._virtual_portfolio.process_fill(order)

            logger.info(
                f"SHADOW: {side.value} {quantity} {asset} @ ${price:.2f} "
                f"(value: ${order_value:,.0f})"
            )

            # Update state
            self._state.orders_placed += 1
            self._refresh_state_snapshot_from_cache()
            self._prune_history()  # Memory leak fix
            self._save_state()
            self.record_event(
                "shadow_order_filled",
                **self._order_event_payload(order),
                order_value=order_value,
            )

            return order

        # === Execute ===
        logger.info(f"SafeBroker: Submitting {side.value} {quantity} {asset}")
        order = await self._broker.submit_order_async(
            asset, quantity, side, order_type, limit_price, stop_price, **kwargs
        )

        # Update state
        self._state.orders_placed += 1
        self._recent_orders.append((time.time(), asset, float(quantity)))
        self._refresh_state_snapshot_from_cache()
        self._prune_history()  # Memory leak fix
        self._save_state()
        self.record_event(
            "order_submitted",
            **self._order_event_payload(order),
            order_value=order_value,
        )

        return order

    # === Risk Check Methods ===

    def _record_market_data(
        self,
        timestamp: datetime,
        data: dict[str, Any],
        context: dict[str, Any],
    ) -> None:
        """Cache the latest market reference for each asset in a feed update."""
        normalized_timestamp = self._normalize_timestamp(timestamp)
        observed_at = datetime.now(UTC)
        latest_prices: dict[str, float] = {}
        get_position = getattr(self._broker, "get_position", None)

        for asset, asset_data in data.items():
            if not isinstance(asset_data, dict):
                continue

            asset_key = asset.upper()
            asset_context = context.get(asset, {}) if isinstance(context, dict) else {}
            if not isinstance(asset_context, dict):
                asset_context = {}

            price = self._extract_reference_price(asset_data, asset_context)
            if price is None:
                continue

            self._latest_market_data[asset_key] = MarketSnapshot(
                timestamp=normalized_timestamp,
                observed_at=observed_at,
                price=price,
            )
            latest_prices[asset_key] = price

            if callable(get_position):
                position = get_position(asset_key)
                if position is not None:
                    position.current_price = price

        if latest_prices:
            self._virtual_portfolio.update_prices(latest_prices)

    def _normalize_timestamp(self, timestamp: datetime) -> datetime:
        """Normalize feed timestamps to UTC for freshness checks."""
        if timestamp.tzinfo is None:
            return timestamp.replace(tzinfo=UTC)
        return timestamp.astimezone(UTC)

    def _extract_reference_price(
        self, asset_data: dict[str, Any], asset_context: dict[str, Any]
    ) -> float | None:
        """Extract a tradeable reference price from feed payloads."""
        for key in ("close", "price"):
            value = asset_data.get(key)
            if value is None:
                continue
            try:
                price = float(value)
            except (TypeError, ValueError):
                continue
            if price > 0:
                return price

        bid = asset_data.get("bid")
        ask = asset_data.get("ask")
        if bid is None:
            bid = asset_context.get("bid")
        if ask is None:
            ask = asset_context.get("ask")

        try:
            if bid is not None and ask is not None:
                bid_value = float(bid)
                ask_value = float(ask)
                if bid_value > 0 and ask_value > 0:
                    return (bid_value + ask_value) / 2
        except (TypeError, ValueError):
            pass

        for value in (bid, ask, asset_data.get("open")):
            if value is None:
                continue
            try:
                price = float(value)
            except (TypeError, ValueError):
                continue
            if price > 0:
                return price

        return None

    def _get_market_snapshot(self, asset: str) -> MarketSnapshot | None:
        """Return the latest cached market snapshot for an asset."""
        return self._latest_market_data.get(asset.upper())

    def record_market_snapshot(
        self,
        asset: str,
        price: float,
        timestamp: datetime | None = None,
    ) -> None:
        """Cache a single price observation for the staleness guard.

        **The supported way to keep the cache fresh is the streaming path:**
        a ``Feed`` (e.g. ``IBDataFeed``) emits ticks, ``LiveEngine`` shuttles
        them into ``_record_market_data`` on every bar, and the cache stays
        current automatically. Use that for any continuous-loop deployment.

        This method is the **non-streaming escape hatch** for one-shot flows
        that legitimately have no tick stream in front of the broker:

        - A CLI flatten tool that takes a position list and submits MOC/MARKET
          closeouts.
        - A REST-only broker adapter that fetches quotes synchronously per
          request rather than via a streaming feed.
        - A test harness setting up controlled state.

        It is **not** the right tool inside a notebook that already runs a
        live engine - there the streaming path covers staleness implicitly.
        Reaching for ``record_market_snapshot`` from inside a tick-driven
        flow is a code smell: it usually means the engine isn't actually
        wired up, and the snapshot will go stale silently.

        Args:
            asset: Symbol to record.
            price: Reference price (e.g. last close, mid quote, snapshot
                top-of-book mid). Must be > 0.
            timestamp: Bar/quote timestamp. Defaults to now (UTC). The
                ``observed_at`` field is always set to now so freshness
                checks measure wall-clock age since this call, not since the
                quote was generated.
        """
        if not (price > 0):
            raise ValueError(f"record_market_snapshot: price must be > 0, got {price}")
        observed_at = datetime.now(UTC)
        ts = timestamp if timestamp is not None else observed_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        else:
            ts = ts.astimezone(UTC)
        asset_key = asset.upper()
        self._latest_market_data[asset_key] = MarketSnapshot(
            timestamp=ts,
            observed_at=observed_at,
            price=float(price),
        )
        self._virtual_portfolio.update_prices({asset_key: float(price)})

    def _check_data_staleness(self, asset: str) -> None:
        """Reject orders when the latest known market data is missing or stale."""
        snapshot = self._get_market_snapshot(asset)
        if snapshot is None:
            raise RiskLimitError(f"No market data available for {asset}")

        now = datetime.now(UTC)
        age_seconds = max(0.0, (now - snapshot.observed_at).total_seconds())

        if age_seconds > self.config.max_data_staleness_seconds:
            raise RiskLimitError(
                f"Stale market data for {asset}: {age_seconds:.1f}s old exceeds "
                f"max {self.config.max_data_staleness_seconds:.1f}s"
            )

    async def _check_daily_loss(self) -> None:
        """Reject new orders when the daily-loss limit has been breached."""
        try:
            current_equity = await self.get_account_value_async()
        except Exception:
            return

        if self._state.session_start_equity is None:
            self._state.session_start_equity = current_equity

        daily_loss = max(0.0, self._state.session_start_equity - current_equity)
        self._state.daily_loss = daily_loss

        if daily_loss > self.config.max_daily_loss:
            reason = f"Daily loss ${daily_loss:,.2f} exceeds max ${self.config.max_daily_loss:,.2f}"
            self._activate_kill_switch(reason)
            raise RiskLimitError(reason)

    def _check_asset(self, asset: str) -> None:
        """Check if asset is allowed.

        Args:
            asset: Asset symbol

        Raises:
            RiskLimitError: If asset is blocked or not allowed
        """
        if asset in self.config.blocked_assets:
            raise RiskLimitError(f"Asset {asset} is blocked")
        if self.config.allowed_assets and asset not in self.config.allowed_assets:
            raise RiskLimitError(f"Asset {asset} not in allowed list")

    def _check_duplicate(self, asset: str, quantity: float) -> None:
        """Block duplicate orders within dedup window.

        Args:
            asset: Asset symbol
            quantity: Order quantity

        Raises:
            RiskLimitError: If duplicate order detected
        """
        now = time.time()
        window = self.config.dedup_window_seconds

        # Clean old entries
        self._recent_orders = [(t, a, q) for t, a, q in self._recent_orders if now - t < window]

        # Check for duplicate
        for t, a, q in self._recent_orders:
            if a == asset and abs(q - quantity) < 0.01:
                raise RiskLimitError(
                    f"Duplicate order blocked: {asset} {quantity} (same order {now - t:.1f}s ago)"
                )

    def _check_rate_limit(self) -> None:
        """Check order rate limit.

        Raises:
            RiskLimitError: If rate limit exceeded
        """
        now = time.time()
        self._order_timestamps = [ts for ts in self._order_timestamps if now - ts < 60]
        if len(self._order_timestamps) >= self.config.max_orders_per_minute:
            raise RiskLimitError(f"Rate limit: {self.config.max_orders_per_minute}/min exceeded")
        self._order_timestamps.append(now)

    def _check_order_limits(self, quantity: float, value: float) -> None:
        """Check order size limits.

        Args:
            quantity: Order quantity
            value: Order value

        Raises:
            RiskLimitError: If order limits exceeded
        """
        if abs(quantity) > self.config.max_order_shares:
            raise RiskLimitError(
                f"Order quantity {quantity} exceeds max {self.config.max_order_shares}"
            )
        if value > self.config.max_order_value:
            raise RiskLimitError(
                f"Order value ${value:,.0f} exceeds max ${self.config.max_order_value:,.0f}"
            )

    async def _check_position_limits(
        self, asset: str, quantity: float, order_value: float, side: OrderSide
    ) -> None:
        """Check position size limits.

        Args:
            asset: Asset symbol
            quantity: Order quantity
            order_value: Order value
            side: Order side

        Raises:
            RiskLimitError: If position limits exceeded
        """
        pos = self.get_position(asset)
        current_qty = pos.quantity if pos else 0
        current_value = abs(pos.market_value) if pos else 0
        order_unit_price = order_value / abs(quantity) if quantity else 0.0

        # Projected position
        if side == OrderSide.BUY:
            projected_qty = current_qty + quantity
        else:
            projected_qty = current_qty - quantity

        projected_value = abs(projected_qty) * order_unit_price
        total = sum(abs(p.market_value) for p in self.positions.values()) - current_value

        if projected_value > self.config.max_position_value:
            raise RiskLimitError(
                f"Position value ${projected_value:,.0f} would exceed "
                f"max ${self.config.max_position_value:,.0f}"
            )

        if abs(projected_qty) > self.config.max_position_shares:
            raise RiskLimitError(
                f"Position quantity {projected_qty} would exceed "
                f"max {self.config.max_position_shares}"
            )

        # Total exposure
        if total + projected_value > self.config.max_total_exposure:
            raise RiskLimitError(
                f"Total exposure ${total + projected_value:,.0f} would exceed "
                f"max ${self.config.max_total_exposure:,.0f}"
            )

        # Max positions
        if pos is None and len(self.positions) >= self.config.max_positions:
            raise RiskLimitError(f"Max positions ({self.config.max_positions}) reached")

    async def _check_price_deviation(self, asset: str, limit_price: float) -> None:
        """Fat finger check: reject if limit price too far from market.

        Args:
            asset: Asset symbol
            limit_price: Limit price

        Raises:
            RiskLimitError: If price deviation exceeds limit
        """
        snapshot = self._get_market_snapshot(asset)
        market_price = snapshot.price if snapshot is not None else None

        if market_price is None:
            pos = self.get_position(asset)
            if pos and pos.current_price:
                market_price = pos.current_price

        if market_price is None:
            raise RiskLimitError(f"No market data available for {asset}")

        deviation = abs(limit_price - market_price) / market_price

        if deviation > self.config.max_price_deviation_pct:
            raise RiskLimitError(
                f"Price deviation {deviation:.1%} exceeds max "
                f"{self.config.max_price_deviation_pct:.1%}. "
                f"Limit: ${limit_price:.2f}, Market: ${market_price:.2f}"
            )

    async def _check_drawdown(self) -> None:
        """Check drawdown and activate kill switch if exceeded.

        Raises:
            RiskLimitError: If drawdown exceeds limit
        """
        try:
            current = await self.get_account_value_async()
        except Exception:
            return  # Can't check, skip

        # Update high water mark
        if current > self._state.high_water_mark:
            self._state.high_water_mark = current

        # Calculate drawdown
        if self._state.high_water_mark > 0:
            drawdown = (self._state.high_water_mark - current) / self._state.high_water_mark

            if drawdown > self.config.max_drawdown_pct:
                reason = f"Drawdown {drawdown:.1%} exceeds max {self.config.max_drawdown_pct:.1%}"
                self._activate_kill_switch(reason)
                raise RiskLimitError(reason)

    async def _estimate_price(self, asset: str, limit_price: float | None) -> float:
        """Estimate order fill price.

        Args:
            asset: Asset symbol
            limit_price: Limit price if provided

        Returns:
            Estimated fill price
        """
        if limit_price:
            return limit_price

        snapshot = self._get_market_snapshot(asset)
        if snapshot is not None:
            return snapshot.price

        pos = self.get_position(asset)
        if pos and pos.current_price:
            return pos.current_price
        if pos:
            return pos.entry_price

        raise RiskLimitError(f"No market data available for {asset}")

    # === Kill Switch ===

    def _activate_kill_switch(self, reason: str) -> None:
        """Activate kill switch and persist.

        Args:
            reason: Reason for activation
        """
        logger.critical(f"KILL SWITCH ACTIVATED: {reason}")
        self._state.kill_switch_activated = True
        self._state.kill_switch_reason = reason
        self.config.kill_switch_enabled = True
        self._save_state()
        self.record_event("kill_switch_activated", reason=reason)

    def enable_kill_switch(self, reason: str = "Manual") -> None:
        """Manually enable kill switch.

        Args:
            reason: Reason for activation (default: "Manual")
        """
        self._activate_kill_switch(reason)

    def disable_kill_switch(self) -> None:
        """Manually disable kill switch (use with caution!)."""
        logger.warning("Kill switch DISABLED - proceed with caution!")
        self._state.kill_switch_activated = False
        self._state.kill_switch_reason = ""
        self.config.kill_switch_enabled = False
        self._save_state()
        self.record_event("kill_switch_disabled")

    async def close_all_positions(self) -> list[Order]:
        """Emergency close all positions.

        Returns:
            List of close orders
        """
        logger.warning("EMERGENCY: Closing ALL positions")
        orders = []
        for asset in list(self.positions.keys()):
            order = await self.close_position_async(asset)
            if order:
                orders.append(order)
        return orders

    # === State Persistence ===

    def _load_state(self) -> RiskState:
        """Load state from file or create new.

        Returns:
            RiskState object
        """
        today = date.today().isoformat()
        path = Path(self.config.state_file)

        if path.exists():
            try:
                data = json.loads(path.read_text())
                state = RiskState(**data)

                # Reset if new day
                if state.date != today:
                    logger.info("New trading day - resetting daily counters")
                    state.date = today
                    state.daily_loss = 0.0
                    state.orders_placed = 0
                    state.session_start_equity = None
                    # Keep kill switch state - must be manually reset!

                return state
            except Exception as e:
                logger.warning(f"Failed to load risk state: {e}")

        return RiskState(date=today)

    def _save_state(self) -> None:
        """Save state to file using atomic write (Gemini v2 fix).

        Writes to .tmp file first, then atomically replaces the target.
        Prevents corruption if process dies mid-write.
        """
        try:
            if self.config.shadow_mode:
                self._state.shadow_portfolio = self._virtual_portfolio.to_state()
            path = Path(self.config.state_file)
            tmp_path = path.with_suffix(".json.tmp")

            # Write to temp file
            tmp_path.write_text(json.dumps(self._state.to_dict(), indent=2))

            # Atomic replace (POSIX and Windows)
            os.replace(tmp_path, path)
        except Exception as e:
            logger.error(f"Failed to save risk state: {e}")

    def _journal_path(self) -> Path:
        """Return the configured JSONL journal path."""
        if self.config.journal_file is not None:
            return Path(self.config.journal_file)

        state_path = Path(self.config.state_file)
        suffix = state_path.suffix or ".json"
        return state_path.with_name(f"{state_path.stem}-journal{suffix}l")

    def record_event(self, event: str, **payload: Any) -> None:
        """Append a structured runtime event to the execution journal."""
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
            "shadow_mode": self.config.shadow_mode,
            "kill_switch": self._state.kill_switch_activated,
            "orders_placed": self._state.orders_placed,
            "daily_loss": self._state.daily_loss,
            "payload": self._json_safe(payload),
        }

        try:
            path = self._journal_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, sort_keys=True) + "\n")
        except Exception as exc:
            logger.error("Failed to append journal entry: %s", exc)

    def _json_safe(self, value: Any) -> Any:
        """Convert runtime payloads into JSON-serializable primitives."""
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            return value.isoformat()
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, dict):
            return {str(key): self._json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self._json_safe(item) for item in value]
        return value

    def _order_identifier(self, order: Order | None) -> str | None:
        """Return the best available local identifier for an order."""
        if order is None:
            return None
        for attr in ("order_id", "id"):
            value = getattr(order, attr, None)
            if value is not None:
                return str(value)
        return None

    def _order_event_payload(self, order: Order) -> dict[str, Any]:
        """Serialize an order into a compact journal payload."""
        payload = {
            "order_id": self._order_identifier(order),
            "asset": order.asset,
            "side": order.side.value,
            "quantity": float(order.quantity),
            "order_type": order.order_type.value,
            "status": order.status.value if order.status is not None else None,
        }
        if order.limit_price is not None:
            payload["limit_price"] = float(order.limit_price)
        if order.stop_price is not None:
            payload["stop_price"] = float(order.stop_price)
        if order.filled_price is not None:
            payload["filled_price"] = float(order.filled_price)
        if order.filled_quantity is not None:
            payload["filled_quantity"] = float(order.filled_quantity)
        return payload

    def _pending_order_by_id(self, order_id: str) -> Order | None:
        """Find a pending order by either order_id or id attribute."""
        for order in self.pending_orders:
            if self._order_identifier(order) == order_id:
                return order
        return None

    def _prune_history(self) -> None:
        """Clean up old entries to prevent memory leaks (Gemini v2 fix).

        Called on every order to ensure cleanup happens even if
        duplicate checking is disabled.
        """
        now = time.time()

        # Prune order timestamps (older than 1 minute)
        self._order_timestamps = [ts for ts in self._order_timestamps if now - ts < 60]

        # Prune recent orders (older than dedup window, max 1 hour)
        max_age = max(self.config.dedup_window_seconds, 3600)
        self._recent_orders = [(t, a, q) for t, a, q in self._recent_orders if now - t < max_age]

    def _serialize_positions(self, positions: dict[str, Position]) -> dict[str, float]:
        """Serialize positions into a compact persisted snapshot."""
        snapshot: dict[str, float] = {}
        for asset, position in positions.items():
            try:
                quantity = float(position.quantity)
            except (AttributeError, TypeError, ValueError):
                continue
            if quantity != 0:
                snapshot[asset.upper()] = quantity
        return dict(sorted(snapshot.items()))

    def _serialize_pending_orders(self, orders: list[Order]) -> list[dict[str, Any]]:
        """Serialize pending orders for persistence and reconciliation."""
        snapshot: list[dict[str, Any]] = []
        for order in orders:
            try:
                entry = {
                    "asset": order.asset.upper(),
                    "side": order.side.value,
                    "quantity": float(order.quantity),
                    "order_type": order.order_type.value,
                }
            except (AttributeError, TypeError, ValueError):
                continue
            if order.limit_price is not None:
                entry["limit_price"] = float(order.limit_price)
            if order.stop_price is not None:
                entry["stop_price"] = float(order.stop_price)
            snapshot.append(entry)
        snapshot.sort(key=self._pending_order_fingerprint)
        return snapshot

    def _pending_order_fingerprint(self, order: dict[str, Any]) -> tuple[Any, ...]:
        """Return a stable fingerprint for a serialized pending order."""
        return (
            order.get("asset", ""),
            order.get("side", ""),
            float(order.get("quantity", 0.0)),
            order.get("order_type", ""),
            order.get("limit_price"),
            order.get("stop_price"),
        )

    def _set_state_snapshot(
        self,
        positions: dict[str, Position],
        pending_orders: list[Order],
    ) -> None:
        """Persist the latest known broker snapshot into risk state."""
        self._state.persisted_positions = self._serialize_positions(positions)
        self._state.persisted_pending_orders = self._serialize_pending_orders(pending_orders)

    def _refresh_state_snapshot_from_cache(self) -> None:
        """Refresh persisted snapshots from the best local broker cache available."""
        if self.config.shadow_mode:
            self._set_state_snapshot(self._virtual_portfolio.positions, [])
            return

        positions = getattr(self._broker, "positions", {})
        pending_orders = getattr(self._broker, "pending_orders", [])
        if isinstance(positions, dict) and isinstance(pending_orders, list):
            self._set_state_snapshot(positions, pending_orders)

    async def _capture_runtime_snapshot_async(self) -> tuple[dict[str, Position], list[Order]]:
        """Capture current positions and pending orders from the live broker."""
        if self.config.shadow_mode:
            return self._virtual_portfolio.positions, []

        try:
            positions = await self._broker.get_positions_async()
        except Exception:
            positions = getattr(self._broker, "positions", {})

        try:
            pending_orders = await self._broker.get_pending_orders_async()
        except Exception:
            pending_orders = getattr(self._broker, "pending_orders", [])

        if not isinstance(positions, dict):
            positions = {}
        if not isinstance(pending_orders, list):
            pending_orders = []
        return positions, pending_orders

    def _build_reconciliation_report(
        self,
        live_positions: dict[str, Position],
        live_pending_orders: list[Order],
    ) -> dict[str, Any]:
        """Compare persisted broker state against the live broker snapshot."""
        persisted_positions = dict(self._state.persisted_positions)
        persisted_pending_orders = list(self._state.persisted_pending_orders)
        live_position_snapshot = self._serialize_positions(live_positions)
        live_pending_snapshot = self._serialize_pending_orders(live_pending_orders)

        missing_positions = {
            asset: quantity
            for asset, quantity in persisted_positions.items()
            if asset not in live_position_snapshot
        }
        unexpected_positions = {
            asset: quantity
            for asset, quantity in live_position_snapshot.items()
            if asset not in persisted_positions
        }
        quantity_mismatches = {
            asset: {
                "persisted": persisted_positions[asset],
                "live": live_position_snapshot[asset],
            }
            for asset in sorted(persisted_positions.keys() & live_position_snapshot.keys())
            if abs(persisted_positions[asset] - live_position_snapshot[asset]) > 1e-9
        }

        live_pending_lookup = {
            self._pending_order_fingerprint(order): order for order in live_pending_snapshot
        }
        persisted_pending_lookup = {
            self._pending_order_fingerprint(order): order for order in persisted_pending_orders
        }
        missing_pending_orders = [
            persisted_pending_lookup[key]
            for key in sorted(persisted_pending_lookup.keys() - live_pending_lookup.keys())
        ]
        unexpected_pending_orders = [
            live_pending_lookup[key]
            for key in sorted(live_pending_lookup.keys() - persisted_pending_lookup.keys())
        ]

        clean = not any(
            (
                missing_positions,
                unexpected_positions,
                quantity_mismatches,
                missing_pending_orders,
                unexpected_pending_orders,
            )
        )
        return {
            "checked_at": datetime.now(UTC).isoformat(),
            "clean": clean,
            "persisted_positions": persisted_positions,
            "live_positions": live_position_snapshot,
            "missing_positions": missing_positions,
            "unexpected_positions": unexpected_positions,
            "quantity_mismatches": quantity_mismatches,
            "persisted_pending_orders": persisted_pending_orders,
            "live_pending_orders": live_pending_snapshot,
            "missing_pending_orders": missing_pending_orders,
            "unexpected_pending_orders": unexpected_pending_orders,
        }

    def _log_reconciliation_report(self, report: dict[str, Any]) -> None:
        """Log a concise startup reconciliation summary."""
        if report["clean"]:
            logger.info("SafeBroker reconciliation clean")
            return

        issues: list[str] = []
        if report["missing_positions"]:
            issues.append(f"missing_positions={sorted(report['missing_positions'])}")
        if report["unexpected_positions"]:
            issues.append(f"unexpected_positions={sorted(report['unexpected_positions'])}")
        if report["quantity_mismatches"]:
            issues.append(f"quantity_mismatches={sorted(report['quantity_mismatches'])}")
        if report["missing_pending_orders"]:
            issues.append(f"missing_pending_orders={len(report['missing_pending_orders'])}")
        if report["unexpected_pending_orders"]:
            issues.append(f"unexpected_pending_orders={len(report['unexpected_pending_orders'])}")
        logger.warning("SafeBroker reconciliation mismatch: %s", ", ".join(issues))

    async def preview_reconciliation_async(self) -> dict[str, Any]:
        """Build the current runtime reconciliation report without mutating state."""
        live_positions, live_pending_orders = await self._capture_runtime_snapshot_async()
        return self._build_reconciliation_report(live_positions, live_pending_orders)

    async def preflight_async(self) -> dict[str, Any]:
        """Probe broker reachability and startup reconciliation without persisting state."""
        await self._broker.connect()
        try:
            account_value = await self.get_account_value_async()
            cash = await self.get_cash_async()
            report = await self.preview_reconciliation_async()
            connected = await self._broker.is_connected_async()
            result = {
                "broker_connected": connected,
                "account_value": account_value,
                "cash": cash,
                "kill_switch_activated": self._state.kill_switch_activated,
                "kill_switch_reason": self._state.kill_switch_reason,
                "reconciliation": report,
                "journal_file": str(self._journal_path()),
                "state_file": self.config.state_file,
                "passed": connected
                and not self._state.kill_switch_activated
                and (report["clean"] or not self.config.fail_on_reconciliation_mismatch),
            }
            self.record_event(
                "preflight_completed",
                passed=result["passed"],
                reconciliation_clean=report["clean"],
                broker_connected=connected,
            )
            return result
        finally:
            await self._broker.disconnect()

    # === Broker Connection Methods (passthrough) ===

    async def connect(self) -> None:
        """Connect to broker and reconcile persisted state."""
        await self._broker.connect()
        report = await self.preview_reconciliation_async()
        self._last_reconciliation_report = report
        self._log_reconciliation_report(report)
        if report["clean"]:
            self.record_event("reconciliation_clean")
        else:
            self.record_event("reconciliation_mismatch", report=report)

        if (
            not report["clean"]
            and self.config.fail_on_reconciliation_mismatch
            and not self.config.shadow_mode
        ):
            self.record_event("reconciliation_blocked_startup", report=report)
            await self._broker.disconnect()
            raise ReconciliationMismatchError("Startup reconciliation mismatch blocked connect()")

        live_positions, live_pending_orders = await self._capture_runtime_snapshot_async()
        self._set_state_snapshot(live_positions, live_pending_orders)
        self.record_event("broker_connected")

    async def disconnect(self) -> None:
        """Disconnect from broker and save state."""
        try:
            live_positions, live_pending_orders = await self._capture_runtime_snapshot_async()
            self._set_state_snapshot(live_positions, live_pending_orders)
        except Exception as e:
            logger.warning(f"Failed to capture broker snapshot during disconnect: {e}")
            self._refresh_state_snapshot_from_cache()
        self._save_state()
        await self._broker.disconnect()
        self.record_event("broker_disconnected")

    async def is_connected_async(self) -> bool:
        """Check if connected (async)."""
        return await self._broker.is_connected_async()

    async def get_positions_async(self) -> dict[str, Position]:
        """Get all positions (async).

        Returns:
            Dictionary mapping asset symbol to Position
        """
        if self.config.shadow_mode:
            return self._virtual_portfolio.positions
        return await self._broker.get_positions_async()

    async def get_pending_orders_async(self, asset: str | None = None) -> list[Order]:
        """Get pending orders (async).

        Returns:
            List of pending orders
        """
        if asset is None:
            return await self._broker.get_pending_orders_async()
        return await self._broker.get_pending_orders_async(asset)

    async def get_position_async(self, asset: str) -> Position | None:
        """Get position (async).

        Args:
            asset: Asset symbol

        Returns:
            Position object or None
        """
        if self.config.shadow_mode:
            return self._virtual_portfolio.positions.get(asset)
        return await self._broker.get_position_async(asset)
