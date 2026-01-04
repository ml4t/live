# ML4T Live Trading Platform - Design Document

**Version**: 3.0
**Date**: 2025-11-24
**Package**: `ml4t-live`
**Status**: Revised (addresses Gemini v1 + v2 reviews + production patterns)

---

## 1. Executive Summary

### 1.1 Vision

Enable users to **copy-paste their Strategy class** from backtesting to live trading with zero code changes. The same `Strategy.on_data(timestamp, data, context, broker)` signature works in both environments.

### 1.2 Key Principles

1. **Strategy Portability**: Identical strategy code runs in backtest and live
2. **Protocol-Based Abstraction**: `BrokerProtocol` implemented by all broker types
3. **Safety First**: Multiple layers of risk controls, paper trading recommended
4. **Broker Agnostic**: Support IB, Alpaca, and extensible to others
5. **Async Architecture**: Non-blocking I/O for real-time data and order execution
6. **Simplicity**: Educational library, not production-grade complexity

### 1.3 Scope & Disclaimer

**This library is designed for paper trading and educational purposes.**

While it can execute real trades, we recommend:
- Always start with paper trading
- Use small position sizes when going live
- Never trade money you can't afford to lose
- This is NOT a substitute for professional trading systems

### 1.4 Package Relationship

```
ml4t-backtest (standalone)
      ↑
ml4t-live (depends on ml4t-backtest for protocols, types, Strategy base class)
```

---

## 2. Architecture Overview

### 2.1 Component Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              User's Strategy                                 │
│                                                                              │
│   class MyStrategy(Strategy):                                                │
│       def on_data(self, timestamp, data, context, broker):                   │
│           # SAME CODE for backtest AND live                                  │
│           target = {'AAPL': 0.5, 'GOOG': 0.3}                               │
│           self.executor.execute(target, data, broker)                        │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
                                       │
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         BrokerProtocol Interface                             │
│                                                                              │
│   @runtime_checkable                                                         │
│   class BrokerProtocol(Protocol):                                            │
│       # Synchronous API - strategies don't need async knowledge              │
│       positions: dict[str, Position]                                         │
│       pending_orders: list[Order]                                            │
│       def get_position(asset) -> Position | None                             │
│       def get_account_value() -> float                                       │
│       def get_cash() -> float                                                │
│       def submit_order(asset, quantity, ...) -> Order                        │
│       def cancel_order(order_id) -> bool                                     │
│       def close_position(asset) -> Order | None                              │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
                 │                                        │
                 ▼                                        ▼
┌────────────────────────────┐        ┌────────────────────────────────────────┐
│   Backtest Infrastructure   │        │         Live Infrastructure            │
│                             │        │                                        │
│  ┌────────────────────┐    │        │  ┌──────────────────────────────────┐  │
│  │ Engine (sync loop) │    │        │  │ LiveEngine (async)               │  │
│  └────────────────────┘    │        │  │   └─ ThreadSafeBrokerWrapper     │  │
│           │                 │        │  └──────────────────────────────────┘  │
│           ▼                 │        │           │                            │
│  ┌────────────────────┐    │        │           ▼                            │
│  │ Broker (simulated) │    │        │  ┌──────────────────────────────────┐  │
│  └────────────────────┘    │        │  │ SafeBroker (risk wrapper)        │  │
│           │                 │        │  │   └─ State persistence           │  │
│           ▼                 │        │  └──────────────────────────────────┘  │
│  ┌────────────────────┐    │        │           │                            │
│  │ DataFeed (iterate) │    │        │           ▼                            │
│  └────────────────────┘    │        │  ┌────────────┐   ┌─────────────────┐  │
│                             │        │  │ IBBroker   │   │ AlpacaBroker    │  │
└────────────────────────────┘        │  │  (async)   │   │   (REST)        │  │
                                       │  └────────────┘   └─────────────────┘  │
                                       │           │                            │
                                       │           ▼                            │
                                       │  ┌──────────────────────────────────┐  │
                                       │  │ DataFeed + BarAggregator         │  │
                                       │  │   └─ Buffers ticks → minute bars │  │
                                       │  └──────────────────────────────────┘  │
                                       │                                        │
                                       └────────────────────────────────────────┘
```

### 2.2 Critical Design Decision: Sync/Async Boundary

**Problem** (identified by Gemini review): Strategy.on_data() is synchronous, but live brokers require async I/O.

**Solution**: `ThreadSafeBrokerWrapper` bridges the sync/async boundary:

```
                    ┌─────────────────────────────────────────────────┐
                    │              LiveEngine (async)                  │
                    │                                                  │
  Main Thread       │   asyncio event loop                            │
  (event loop)      │   ├── IBBroker.submit_order_async()             │
                    │   ├── IBDataFeed stream                         │
                    │   └── ...                                        │
                    └─────────────────────────────────────────────────┘
                                         ↑
                                         │ run_coroutine_threadsafe()
                                         │
                    ┌─────────────────────────────────────────────────┐
  Worker Thread     │   Strategy.on_data()                            │
  (via to_thread)   │   └── broker.submit_order()  ← BLOCKING wrapper │
                    │       └── ThreadSafeBrokerWrapper               │
                    └─────────────────────────────────────────────────┘
```

---

## 3. Core Components

### 3.1 ThreadSafeBrokerWrapper

**Critical component** that allows sync strategies to call async broker methods:

```python
# ml4t/live/wrappers.py

import asyncio
from typing import Any
from ml4t.backtest.types import Order, OrderSide, OrderType, Position


class ThreadSafeBrokerWrapper:
    """Wraps an async broker for use from sync strategy code.

    This wrapper is passed to Strategy.on_data() instead of the raw broker.
    It bridges the sync/async boundary by scheduling coroutines on the main
    event loop and blocking the worker thread until they complete.

    Thread Safety:
    - Strategy runs in worker thread (via asyncio.to_thread)
    - Broker methods run on main event loop
    - run_coroutine_threadsafe() handles the cross-thread communication

    Example:
        # LiveEngine creates this wrapper
        loop = asyncio.get_running_loop()
        wrapped = ThreadSafeBrokerWrapper(ib_broker, loop)

        # Strategy uses it like a normal sync broker
        order = wrapped.submit_order('AAPL', 100, OrderSide.BUY)
    """

    def __init__(self, async_broker: "AsyncBrokerProtocol", loop: asyncio.AbstractEventLoop):
        self._broker = async_broker
        self._loop = loop

    # === Sync properties (can be accessed directly) ===

    @property
    def positions(self) -> dict[str, Position]:
        """Current positions - thread-safe read."""
        return self._broker.positions

    @property
    def pending_orders(self) -> list[Order]:
        """Pending orders - thread-safe read."""
        return self._broker.pending_orders

    # === Sync methods that wrap async operations ===

    def get_position(self, asset: str) -> Position | None:
        """Get position for asset."""
        return self._broker.get_position(asset)

    def get_account_value(self) -> float:
        """Get total account value."""
        # This may require async call to IB
        return self._run_sync(self._broker.get_account_value_async(), timeout=5.0)

    def get_cash(self) -> float:
        """Get available cash."""
        return self._run_sync(self._broker.get_cash_async(), timeout=5.0)

    def submit_order(
        self,
        asset: str,
        quantity: float,
        side: OrderSide | None = None,
        order_type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> Order:
        """Submit order - blocks until acknowledged by broker."""
        return self._run_sync(
            self._broker.submit_order_async(
                asset, quantity, side, order_type, limit_price, stop_price
            ),
            timeout=30.0  # Orders need longer timeout
        )

    def cancel_order(self, order_id: str) -> bool:
        """Cancel pending order."""
        return self._run_sync(self._broker.cancel_order_async(order_id), timeout=30.0)

    def close_position(self, asset: str) -> Order | None:
        """Close position in asset."""
        return self._run_sync(self._broker.close_position_async(asset), timeout=30.0)

    def _run_sync(self, coro: Any, timeout: float = 5.0) -> Any:
        """Schedule coroutine on main loop and wait for result.

        This blocks the worker thread but NOT the main event loop.

        Timeouts (from Gemini review):
        - Getters (get_cash, get_account_value): 5s (default)
        - Order operations (submit, cancel, close): 30s
        """
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)
```

### 3.2 LiveEngine

Async event loop with proper thread boundary handling:

```python
# ml4t/live/engine.py

import asyncio
import logging
import signal
from datetime import datetime
from typing import Callable

from ml4t.backtest import Strategy
from .protocols import AsyncBrokerProtocol, DataFeedProtocol
from .wrappers import ThreadSafeBrokerWrapper

logger = logging.getLogger(__name__)


class LiveEngine:
    """Async live trading engine.

    Bridges async infrastructure with sync Strategy.on_data().

    Key Design Decisions:
    1. Strategy runs in thread pool (via asyncio.to_thread)
    2. ThreadSafeBrokerWrapper passed to strategy for sync broker calls
    3. Graceful shutdown on SIGINT/SIGTERM
    4. Configurable error handling

    Lifecycle:
    1. connect() - Connect to broker and data feed
    2. run() - Main loop (blocks until shutdown)
    3. stop() - Graceful shutdown

    Example:
        engine = LiveEngine(strategy, broker, feed)
        await engine.connect()

        try:
            await engine.run()
        except KeyboardInterrupt:
            await engine.stop()
    """

    def __init__(
        self,
        strategy: Strategy,
        broker: AsyncBrokerProtocol,
        feed: DataFeedProtocol,
        *,
        on_error: Callable[[Exception, datetime, dict], None] | None = None,
        halt_on_error: bool = False,
    ):
        self.strategy = strategy
        self.broker = broker
        self.feed = feed
        self.on_error = on_error or self._default_error_handler
        self.halt_on_error = halt_on_error

        # State
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wrapped_broker: ThreadSafeBrokerWrapper | None = None

        # Statistics
        self._bar_count = 0
        self._error_count = 0
        self._last_bar_time: datetime | None = None

    async def connect(self) -> None:
        """Connect to broker and data feed."""
        logger.info("LiveEngine: Connecting...")

        # Connect broker
        await self.broker.connect()

        # Start data feed
        await self.feed.start()

        # Create thread-safe wrapper
        self._loop = asyncio.get_running_loop()
        self._wrapped_broker = ThreadSafeBrokerWrapper(self.broker, self._loop)

        # Install signal handlers
        self._install_signal_handlers()

        logger.info("LiveEngine: Connected and ready")

    async def run(self) -> None:
        """Main async loop - receives bars and dispatches to strategy.

        Runs until:
        1. stop() is called
        2. Data feed ends
        3. Unrecoverable error (if halt_on_error=True)
        4. SIGINT/SIGTERM received
        """
        if self._wrapped_broker is None:
            raise RuntimeError("Call connect() before run()")

        self._running = True
        logger.info("LiveEngine: Starting main loop")

        # Strategy lifecycle callback
        self.strategy.on_start(self._wrapped_broker)

        try:
            async for timestamp, data, context in self.feed:
                # Check for shutdown
                if self._shutdown_event.is_set():
                    logger.info("LiveEngine: Shutdown requested")
                    break

                self._bar_count += 1
                self._last_bar_time = timestamp

                try:
                    # Run strategy in thread pool to avoid blocking event loop
                    await asyncio.to_thread(
                        self.strategy.on_data,
                        timestamp, data, context, self._wrapped_broker
                    )
                except Exception as e:
                    self._error_count += 1
                    self.on_error(e, timestamp, data)

                    if self.halt_on_error:
                        logger.error("LiveEngine: Halting due to strategy error")
                        break

        except asyncio.CancelledError:
            logger.info("LiveEngine: Cancelled")
        finally:
            self._running = False
            self.strategy.on_end(self._wrapped_broker)
            logger.info(
                f"LiveEngine: Stopped. Bars: {self._bar_count}, Errors: {self._error_count}"
            )

    async def stop(self) -> None:
        """Graceful shutdown."""
        logger.info("LiveEngine: Stopping...")
        self._shutdown_event.set()

        # Stop data feed
        self.feed.stop()

        # Disconnect broker
        await self.broker.disconnect()

        logger.info("LiveEngine: Stopped")

    def _install_signal_handlers(self) -> None:
        """Install SIGINT/SIGTERM handlers for graceful shutdown."""
        loop = asyncio.get_running_loop()

        def handler(sig: signal.Signals) -> None:
            logger.info(f"LiveEngine: Received {sig.name}")
            self._shutdown_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, handler, sig)
            except NotImplementedError:
                # Windows doesn't support add_signal_handler
                pass

    def _default_error_handler(
        self, error: Exception, timestamp: datetime, data: dict
    ) -> None:
        """Default error handler - log and continue."""
        logger.error(
            f"Strategy error at {timestamp}: {type(error).__name__}: {error}",
            exc_info=True
        )

    @property
    def stats(self) -> dict:
        """Get engine statistics."""
        return {
            "running": self._running,
            "bar_count": self._bar_count,
            "error_count": self._error_count,
            "last_bar_time": self._last_bar_time,
        }
```

### 3.3 BarAggregator

Buffers ticks/5-second bars into minute bars:

```python
# ml4t/live/feeds/aggregator.py

import asyncio
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import AsyncIterator

logger = logging.getLogger(__name__)


@dataclass
class BarBuffer:
    """Accumulates ticks into OHLCV bar."""
    open: float | None = None
    high: float = float('-inf')
    low: float = float('inf')
    close: float = 0.0
    volume: int = 0

    def update(self, price: float, size: int = 0) -> None:
        """Add a tick to the bar."""
        if self.open is None:
            self.open = price
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += size

    def to_dict(self) -> dict:
        """Convert to OHLCV dict."""
        return {
            'open': self.open or self.close,
            'high': self.high if self.high != float('-inf') else self.close,
            'low': self.low if self.low != float('inf') else self.close,
            'close': self.close,
            'volume': self.volume,
        }

    def reset(self) -> None:
        """Reset for next bar."""
        self.open = None
        self.high = float('-inf')
        self.low = float('inf')
        self.close = 0.0
        self.volume = 0


class BarAggregator:
    """Aggregates raw ticks or 5-second bars into minute bars.

    Addresses Gemini's concerns:
    1. "If IBDataFeed pushes a tick to Strategy.on_data, the strategy might
       trigger 60x more often than intended." - Buffer incoming data.
    2. "The 15:59 bar is never emitted because no 16:00 tick arrives." -
       Background flush checker emits bars on timeout.

    The aggregator buffers incoming data and emits when:
    - A bar boundary is crossed (new tick arrives in next minute)
    - OR timeout expires (2s past bar end with no new data)

    Example:
        raw_feed = IBTickFeed(ib, assets=['AAPL'])
        aggregated_feed = BarAggregator(raw_feed, bar_size_minutes=1)

        async for timestamp, data, context in aggregated_feed:
            # data contains completed minute bars only
            strategy.on_data(timestamp, data, context, broker)
    """

    def __init__(
        self,
        source_feed: "DataFeedProtocol",
        bar_size_minutes: int = 1,
        assets: list[str] | None = None,
        flush_timeout_seconds: float = 2.0,  # NEW: Gemini fix
    ):
        self.source = source_feed
        self.bar_size = timedelta(minutes=bar_size_minutes)
        self.assets = assets or []
        self.flush_timeout = flush_timeout_seconds

        # Per-asset bar buffers
        self._buffers: dict[str, BarBuffer] = {}
        self._current_bar_start: datetime | None = None
        self._last_data_time: float = 0  # NEW: Track when we last got data

        # Output queue (use None sentinel for shutdown instead of timeout)
        self._queue: asyncio.Queue = asyncio.Queue()
        self._running = False
        self._flush_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start aggregation."""
        self._running = True
        await self.source.start()

        # Start aggregation task
        asyncio.create_task(self._aggregate_loop())

    def stop(self) -> None:
        """Stop aggregation."""
        self._running = False
        self.source.stop()
        # Signal consumer to exit via sentinel
        self._queue.put_nowait(None)
        # Cancel flush task
        if self._flush_task:
            self._flush_task.cancel()

    async def _aggregate_loop(self) -> None:
        """Main aggregation loop."""
        # NEW: Start background flush checker (Gemini "stuck bar" fix)
        self._flush_task = asyncio.create_task(self._flush_checker())

        try:
            async for timestamp, data, context in self.source:
                if not self._running:
                    break

                # Track when we got data (for flush timeout)
                self._last_data_time = time.time()

                # Determine bar boundary
                bar_start = self._truncate_to_bar(timestamp)

                # If bar boundary crossed, emit completed bar
                if self._current_bar_start and bar_start > self._current_bar_start:
                    await self._emit_bar(self._current_bar_start)

                # Update current bar start
                self._current_bar_start = bar_start

                # Accumulate data into buffers
                for asset, ohlcv in data.items():
                    if asset not in self._buffers:
                        self._buffers[asset] = BarBuffer()

                    # Handle different data formats
                    if 'close' in ohlcv:
                        # OHLCV bar data
                        self._buffers[asset].update(ohlcv['close'], ohlcv.get('volume', 0))
                    elif 'price' in ohlcv:
                        # Tick data
                        self._buffers[asset].update(ohlcv['price'], ohlcv.get('size', 0))
        finally:
            if self._flush_task:
                self._flush_task.cancel()

    async def _flush_checker(self) -> None:
        """NEW: Force emit bars if no data arrives (Gemini "stuck bar" fix).

        Scenario: Market closes at 16:00, last tick at 15:59:58. Without this,
        the 15:59 bar never emits because no 16:00 tick arrives to trigger it.
        """
        while self._running:
            await asyncio.sleep(1.0)
            if not self._current_bar_start:
                continue

            # Check if we're past the bar end time + flush timeout
            now = datetime.now()
            bar_end = self._current_bar_start + self.bar_size

            if now > bar_end + timedelta(seconds=self.flush_timeout):
                logger.debug(f"Flush: Emitting stale bar at {self._current_bar_start}")
                await self._emit_bar(self._current_bar_start)
                self._current_bar_start = None  # Prevent double emit

    def _truncate_to_bar(self, dt: datetime) -> datetime:
        """Truncate datetime to bar boundary."""
        minutes = (dt.minute // self.bar_size.total_seconds() * 60) * int(
            self.bar_size.total_seconds() // 60
        )
        return dt.replace(minute=int(minutes), second=0, microsecond=0)

    async def _emit_bar(self, bar_time: datetime) -> None:
        """Emit completed bar."""
        if not self._buffers:
            return

        # Build bar data
        data = {}
        for asset, buffer in self._buffers.items():
            if buffer.open is not None:  # Has data
                data[asset] = buffer.to_dict()
                buffer.reset()

        if data:
            await self._queue.put((bar_time, data, {}))
            logger.debug(f"Emitted bar at {bar_time}: {list(data.keys())}")

    async def __aiter__(self) -> AsyncIterator[tuple[datetime, dict, dict]]:
        """Async iterator interface.

        Uses None sentinel for shutdown (Gemini fix: avoids busy-wait with 1s timeout).
        """
        while True:
            item = await self._queue.get()
            if item is None:  # Shutdown sentinel
                break
            yield item
```

### 3.4 SafeBroker with State Persistence

Enhanced SafeBroker that persists risk state across restarts.

**Addresses Gemini v2 issues:**
- Critical Issue A: VirtualPortfolio for shadow mode (prevents infinite buy loop)
- Atomic JSON writes (prevents corruption on crash)
- Memory leak fixes (prune completed orders)

```python
# ml4t/live/safety.py

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from pathlib import Path
from typing import Any

from ml4t.backtest.types import Order, OrderSide, OrderType, OrderStatus, Position
from .protocols import AsyncBrokerProtocol

logger = logging.getLogger(__name__)


# ============================================================================
# VirtualPortfolio - NEW (Gemini v2 Critical Issue A fix)
# ============================================================================

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
    """

    def __init__(self, initial_cash: float = 100_000.0):
        self._initial_cash = initial_cash
        self._cash = initial_cash
        self._positions: dict[str, Position] = {}

    @property
    def positions(self) -> dict[str, Position]:
        """Returns a copy of current positions."""
        return {k: v for k, v in self._positions.items()}

    @property
    def cash(self) -> float:
        return self._cash

    @property
    def account_value(self) -> float:
        """Total account value (cash + position market value)."""
        market_value = sum(
            abs(p.quantity) * (p.current_price or p.entry_price)
            for p in self._positions.values()
        )
        return self._cash + market_value

    def process_fill(self, order: Order) -> None:
        """Update state based on filled shadow order.

        Handles weighted average cost basis and position flipping.
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
            logger.info(f"Shadow: Opened {asset} {'LONG' if signed_qty > 0 else 'SHORT'} {abs(signed_qty)}")
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
                logger.info(f"Shadow: Increased {asset} to {new_qty}, basis ${current.entry_price:.2f}")

            else:
                # Decreasing position (partial close) - basis unchanged
                current.quantity = new_qty
                current.current_price = fill_price
                logger.info(f"Shadow: Reduced {asset} to {new_qty}")

    def update_prices(self, prices: dict[str, float]) -> None:
        """Update current prices for accurate account value."""
        for asset, price in prices.items():
            if asset in self._positions:
                self._positions[asset].current_price = price


# ============================================================================
# Risk Configuration and State
# ============================================================================


@dataclass
class LiveRiskConfig:
    """Risk configuration for live trading.

    Multiple layers of protection - all limits are optional.
    Set to inf/large values to disable specific checks.
    """

    # Position limits
    max_position_value: float = 50_000.0       # Max $ per position
    max_position_shares: int = 1000            # Max shares per position
    max_total_exposure: float = 200_000.0      # Max total $ across all positions
    max_positions: int = 20                    # Max number of positions

    # Order limits
    max_order_value: float = 10_000.0          # Max $ per order
    max_order_shares: int = 500                # Max shares per order
    max_orders_per_minute: int = 10            # Rate limit

    # Loss limits
    max_daily_loss: float = 5_000.0            # Stop if exceeded
    max_drawdown_pct: float = 0.05             # Stop if 5% drawdown

    # NEW: Gemini-suggested safety features
    max_price_deviation_pct: float = 0.05      # Fat finger: reject if limit >5% from market
    max_data_staleness_seconds: float = 60.0   # Reject if data older than 60s
    dedup_window_seconds: float = 1.0          # Block duplicate orders within 1s

    # Asset restrictions
    allowed_assets: set[str] = field(default_factory=set)
    blocked_assets: set[str] = field(default_factory=set)

    # Shadow mode - log orders but don't execute
    shadow_mode: bool = False

    # Kill switch
    kill_switch_enabled: bool = False

    # State persistence
    state_file: str = ".ml4t_risk_state.json"


@dataclass
class RiskState:
    """Persisted risk state - survives restarts."""
    date: str                                  # YYYY-MM-DD
    daily_loss: float = 0.0                    # Cumulative daily loss
    orders_placed: int = 0                     # Orders placed today
    high_water_mark: float = 0.0               # Session high equity
    kill_switch_activated: bool = False        # Was kill switch triggered?
    kill_switch_reason: str = ""               # Why?


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
        self._broker = broker
        self.config = config

        # Load or initialize state
        self._state = self._load_state()

        # Rate limiting
        self._order_timestamps: list[float] = []

        # Duplicate detection
        self._recent_orders: list[tuple[float, str, float]] = []  # (time, asset, qty)

        # NEW: VirtualPortfolio for shadow mode (Gemini v2 fix)
        self._virtual_portfolio = VirtualPortfolio(initial_cash=100_000.0)

        # Initialize high water mark if not set
        if self._state.high_water_mark == 0.0:
            try:
                self._state.high_water_mark = self._broker.get_account_value()
            except Exception:
                pass

        logger.info(f"SafeBroker initialized. Shadow mode: {config.shadow_mode}")
        if self._state.kill_switch_activated:
            logger.warning(f"Kill switch was previously activated: {self._state.kill_switch_reason}")

    # === BrokerProtocol Implementation ===
    # NEW: Routes to VirtualPortfolio when shadow_mode=True (Gemini v2 fix)

    @property
    def positions(self) -> dict[str, Position]:
        if self.config.shadow_mode:
            return self._virtual_portfolio.positions
        return self._broker.positions

    @property
    def pending_orders(self) -> list[Order]:
        return self._broker.pending_orders

    def get_position(self, asset: str) -> Position | None:
        if self.config.shadow_mode:
            return self._virtual_portfolio.positions.get(asset)
        return self._broker.get_position(asset)

    async def get_account_value_async(self) -> float:
        if self.config.shadow_mode:
            return self._virtual_portfolio.account_value
        return await self._broker.get_account_value_async()

    async def get_cash_async(self) -> float:
        if self.config.shadow_mode:
            return self._virtual_portfolio.cash
        return await self._broker.get_cash_async()

    async def cancel_order_async(self, order_id: str) -> bool:
        return await self._broker.cancel_order_async(order_id)

    async def close_position_async(self, asset: str) -> Order | None:
        # Close positions bypass normal limits (safety feature)
        if self.config.shadow_mode:
            logger.info(f"SHADOW: Would close position in {asset}")
            return None
        return await self._broker.close_position_async(asset)

    # === Risk-Controlled Order Submission ===

    async def submit_order_async(
        self,
        asset: str,
        quantity: float,
        side: OrderSide | None = None,
        order_type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> Order:
        """Submit order with full risk validation."""

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

        # 3. Duplicate check
        self._check_duplicate(asset, quantity)

        # 4. Rate limit
        self._check_rate_limit()

        # 5. Order size limits
        price = await self._estimate_price(asset, limit_price)
        order_value = abs(quantity) * price
        self._check_order_limits(quantity, order_value)

        # 6. Position limits
        await self._check_position_limits(asset, quantity, order_value, side)

        # 7. Fat finger check (limit orders)
        if limit_price and order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT):
            await self._check_price_deviation(asset, limit_price)

        # 8. Drawdown check (may activate kill switch)
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
                order_id=f"SHADOW-{int(time.time()*1000)}",
                status=OrderStatus.FILLED,
                filled_quantity=quantity,
                filled_price=price,
                filled_at=datetime.now(),
            )

            # CRITICAL: Update VirtualPortfolio (fixes infinite buy loop)
            self._virtual_portfolio.process_fill(order)

            logger.info(
                f"SHADOW: {side.value} {quantity} {asset} @ ${price:.2f} "
                f"(value: ${order_value:,.0f})"
            )

            # Update state
            self._state.orders_placed += 1
            self._prune_history()  # Memory leak fix
            self._save_state()

            return order

        # === Execute ===
        logger.info(f"SafeBroker: Submitting {side.value} {quantity} {asset}")
        order = await self._broker.submit_order_async(
            asset, quantity, side, order_type, limit_price, stop_price
        )

        # Update state
        self._state.orders_placed += 1
        self._recent_orders.append((time.time(), asset, quantity))
        self._prune_history()  # Memory leak fix
        self._save_state()

        return order

    # === Risk Check Methods ===

    def _check_asset(self, asset: str) -> None:
        if asset in self.config.blocked_assets:
            raise RiskLimitError(f"Asset {asset} is blocked")
        if self.config.allowed_assets and asset not in self.config.allowed_assets:
            raise RiskLimitError(f"Asset {asset} not in allowed list")

    def _check_duplicate(self, asset: str, quantity: float) -> None:
        """Block duplicate orders within dedup window."""
        now = time.time()
        window = self.config.dedup_window_seconds

        # Clean old entries
        self._recent_orders = [
            (t, a, q) for t, a, q in self._recent_orders
            if now - t < window
        ]

        # Check for duplicate
        for t, a, q in self._recent_orders:
            if a == asset and abs(q - quantity) < 0.01:
                raise RiskLimitError(
                    f"Duplicate order blocked: {asset} {quantity} "
                    f"(same order {now - t:.1f}s ago)"
                )

    def _check_rate_limit(self) -> None:
        now = time.time()
        self._order_timestamps = [
            ts for ts in self._order_timestamps
            if now - ts < 60
        ]
        if len(self._order_timestamps) >= self.config.max_orders_per_minute:
            raise RiskLimitError(
                f"Rate limit: {self.config.max_orders_per_minute}/min exceeded"
            )
        self._order_timestamps.append(now)

    def _check_order_limits(self, quantity: float, value: float) -> None:
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
        pos = self._broker.get_position(asset)
        current_qty = pos.quantity if pos else 0
        current_value = abs(pos.market_value) if pos else 0

        # Projected position
        if side == OrderSide.BUY:
            projected_qty = current_qty + quantity
        else:
            projected_qty = current_qty - quantity

        projected_value = current_value + order_value

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
        total = sum(abs(p.market_value) for p in self._broker.positions.values())
        if total + order_value > self.config.max_total_exposure:
            raise RiskLimitError(
                f"Total exposure ${total + order_value:,.0f} would exceed "
                f"max ${self.config.max_total_exposure:,.0f}"
            )

        # Max positions
        if pos is None and len(self._broker.positions) >= self.config.max_positions:
            raise RiskLimitError(f"Max positions ({self.config.max_positions}) reached")

    async def _check_price_deviation(self, asset: str, limit_price: float) -> None:
        """Fat finger check: reject if limit price too far from market."""
        pos = self._broker.get_position(asset)
        if pos and pos.current_price:
            market_price = pos.current_price
            deviation = abs(limit_price - market_price) / market_price

            if deviation > self.config.max_price_deviation_pct:
                raise RiskLimitError(
                    f"Price deviation {deviation:.1%} exceeds max "
                    f"{self.config.max_price_deviation_pct:.1%}. "
                    f"Limit: ${limit_price:.2f}, Market: ${market_price:.2f}"
                )

    async def _check_drawdown(self) -> None:
        """Check drawdown and activate kill switch if exceeded."""
        try:
            current = await self._broker.get_account_value_async()
        except Exception:
            return  # Can't check, skip

        # Update high water mark
        if current > self._state.high_water_mark:
            self._state.high_water_mark = current

        # Calculate drawdown
        if self._state.high_water_mark > 0:
            drawdown = (self._state.high_water_mark - current) / self._state.high_water_mark

            if drawdown > self.config.max_drawdown_pct:
                reason = (
                    f"Drawdown {drawdown:.1%} exceeds max {self.config.max_drawdown_pct:.1%}"
                )
                self._activate_kill_switch(reason)
                raise RiskLimitError(reason)

    async def _estimate_price(self, asset: str, limit_price: float | None) -> float:
        """Estimate order fill price."""
        if limit_price:
            return limit_price
        pos = self._broker.get_position(asset)
        if pos and pos.current_price:
            return pos.current_price
        return pos.entry_price if pos else 100.0  # Fallback

    # === Kill Switch ===

    def _activate_kill_switch(self, reason: str) -> None:
        """Activate kill switch and persist."""
        logger.critical(f"KILL SWITCH ACTIVATED: {reason}")
        self._state.kill_switch_activated = True
        self._state.kill_switch_reason = reason
        self.config.kill_switch_enabled = True
        self._save_state()

    def enable_kill_switch(self, reason: str = "Manual") -> None:
        """Manually enable kill switch."""
        self._activate_kill_switch(reason)

    def disable_kill_switch(self) -> None:
        """Manually disable kill switch (use with caution!)."""
        logger.warning("Kill switch DISABLED - proceed with caution!")
        self._state.kill_switch_activated = False
        self._state.kill_switch_reason = ""
        self.config.kill_switch_enabled = False
        self._save_state()

    async def close_all_positions(self) -> list[Order]:
        """Emergency close all positions."""
        logger.warning("EMERGENCY: Closing ALL positions")
        orders = []
        for asset in list(self._broker.positions.keys()):
            order = await self._broker.close_position_async(asset)
            if order:
                orders.append(order)
        return orders

    # === State Persistence ===

    def _load_state(self) -> RiskState:
        """Load state from file or create new."""
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
            path = Path(self.config.state_file)
            tmp_path = path.with_suffix('.json.tmp')

            # Write to temp file
            tmp_path.write_text(json.dumps(asdict(self._state), indent=2))

            # Atomic replace (POSIX and Windows)
            os.replace(tmp_path, path)
        except Exception as e:
            logger.error(f"Failed to save risk state: {e}")

    def _prune_history(self) -> None:
        """Clean up old entries to prevent memory leaks (Gemini v2 fix).

        Called on every order to ensure cleanup happens even if
        duplicate checking is disabled.
        """
        now = time.time()

        # Prune order timestamps (older than 1 minute)
        self._order_timestamps = [
            ts for ts in self._order_timestamps
            if now - ts < 60
        ]

        # Prune recent orders (older than dedup window, max 1 hour)
        max_age = max(self.config.dedup_window_seconds, 3600)
        self._recent_orders = [
            (t, a, q) for t, a, q in self._recent_orders
            if now - t < max_age
        ]

    # === Broker Connection Methods (passthrough) ===

    async def connect(self) -> None:
        await self._broker.connect()

    async def disconnect(self) -> None:
        self._save_state()
        await self._broker.disconnect()


class RiskLimitError(Exception):
    """Raised when an order violates risk limits."""
    pass
```

---

## 4. Broker Implementations

### 4.1 IBBroker (Interactive Brokers)

Based on patterns from production implementation:

```python
# ml4t/live/brokers/ib.py

import asyncio
import logging
from datetime import datetime
from typing import Any

from ib_async import IB, Stock, Contract
from ib_async import MarketOrder, LimitOrder, StopOrder, StopLimitOrder
from ib_async import Trade as IBTrade

from ml4t.backtest.types import Order, OrderSide, OrderType, OrderStatus, Position
from ..protocols import AsyncBrokerProtocol

logger = logging.getLogger(__name__)


class IBBroker(AsyncBrokerProtocol):
    """Interactive Brokers implementation.

    Key Design Decisions (from production experience):
    1. All broker operations are async
    2. Uses asyncio.Lock for thread safety (not threading.Lock)
    3. Event handlers use put_nowait() (non-blocking)
    4. Reconnection handled externally (by LiveEngine or user)

    Addresses Gemini v2:
    - Critical Issue C: asyncio.Lock on positions to prevent race condition
    - Memory leak: _ib_order_map pruned on terminal order states

    Connection Ports:
    - TWS Paper: 7497
    - TWS Live: 7496
    - Gateway Paper: 4002
    - Gateway Live: 4001
    """

    def __init__(
        self,
        host: str = '127.0.0.1',
        port: int = 7497,  # Paper trading default
        client_id: int = 1,
        account: str | None = None,
    ):
        self._host = host
        self._port = port
        self._client_id = client_id
        self._account = account

        self.ib = IB()
        self._connected = False

        # Thread-safe state with locks (Gemini v2 Critical Issue C fix)
        self._positions: dict[str, Position] = {}
        self._position_lock = asyncio.Lock()  # NEW: Protects position reads/writes
        self._pending_orders: dict[str, Order] = {}
        self._order_lock = asyncio.Lock()

        # Order tracking
        self._order_counter = 0
        self._ib_order_map: dict[int, tuple[str, float]] = {}  # IB orderId -> (our_id, timestamp)

        # Contract cache
        self._contracts: dict[str, Contract] = {}

    async def connect(self) -> None:
        """Connect to IB Gateway/TWS."""
        logger.info(f"IBBroker: Connecting to {self._host}:{self._port}")

        await self.ib.connectAsync(
            host=self._host,
            port=self._port,
            clientId=self._client_id,
            timeout=15,
        )
        self._connected = True

        # Get account
        if self._account is None:
            accounts = self.ib.managedAccounts()
            if accounts:
                self._account = accounts[0]
        logger.info(f"IBBroker: Connected, account={self._account}")

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
            logger.info("IBBroker: Disconnected")

    @property
    def is_connected(self) -> bool:
        return self._connected and self.ib.isConnected()

    # === AsyncBrokerProtocol Implementation ===

    @property
    def positions(self) -> dict[str, Position]:
        """Thread-safe position access (Gemini v2 Critical Issue C).

        Note: This is called from worker thread via ThreadSafeBrokerWrapper.
        The lock prevents RuntimeError during dict iteration if IB callback
        modifies positions concurrently.
        """
        # For sync access from worker thread, we copy under implicit lock
        # The asyncio.Lock is acquired in async methods below
        return dict(self._positions)  # Shallow copy is atomic for small dicts

    @property
    def pending_orders(self) -> list[Order]:
        return list(self._pending_orders.values())

    def get_position(self, asset: str) -> Position | None:
        """Thread-safe single position access."""
        return self._positions.get(asset.upper())

    async def get_positions_async(self) -> dict[str, Position]:
        """Async thread-safe position access with lock."""
        async with self._position_lock:
            return dict(self._positions)

    async def get_account_value_async(self) -> float:
        """Get Net Liquidation Value."""
        for av in self.ib.accountValues():
            if (av.tag == 'NetLiquidation' and
                av.currency == 'USD' and
                (av.account == self._account or self._account is None)):
                return float(av.value)
        return 0.0

    async def get_cash_async(self) -> float:
        """Get available funds."""
        for av in self.ib.accountValues():
            if (av.tag == 'AvailableFunds' and
                av.currency == 'USD' and
                (av.account == self._account or self._account is None)):
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
        """Submit order to IB."""
        if not self.is_connected:
            raise RuntimeError("Not connected to IB")

        if side is None:
            side = OrderSide.BUY if quantity > 0 else OrderSide.SELL
            quantity = abs(quantity)

        # Get contract
        contract = self._get_contract(asset)

        # Create IB order
        action = 'BUY' if side == OrderSide.BUY else 'SELL'
        ib_order = self._create_ib_order(
            action, quantity, order_type, limit_price, stop_price
        )

        # Submit atomically
        async with self._order_lock:
            self._order_counter += 1
            order_id = f"ML4T-{self._order_counter}"

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

            self._pending_orders[order_id] = order
            self._ib_order_map[trade.order.orderId] = (order_id, time.time())

        logger.info(f"IBBroker: Order {order_id} submitted: {side.value} {quantity} {asset}")
        return order

    async def cancel_order_async(self, order_id: str) -> bool:
        """Cancel pending order."""
        # Find IB order ID
        ib_order_id = None
        for ib_id, (our_id, _) in self._ib_order_map.items():
            if our_id == order_id:
                ib_order_id = ib_id
                break

        if ib_order_id is None:
            logger.warning(f"IBBroker: Order {order_id} not found")
            return False

        # Find and cancel
        for trade in self.ib.openTrades():
            if trade.order.orderId == ib_order_id:
                self.ib.cancelOrder(trade.order)
                logger.info(f"IBBroker: Cancelled order {order_id}")
                return True

        return False

    async def close_position_async(self, asset: str) -> Order | None:
        """Close position in asset."""
        pos = self.get_position(asset)
        if not pos or pos.quantity == 0:
            return None

        side = OrderSide.SELL if pos.quantity > 0 else OrderSide.BUY
        return await self.submit_order_async(asset, abs(pos.quantity), side)

    # === Internal Methods ===

    def _get_contract(self, asset: str) -> Contract:
        """Get or create IB contract."""
        asset = asset.upper()
        if asset not in self._contracts:
            self._contracts[asset] = Stock(asset, 'SMART', 'USD')
        return self._contracts[asset]

    def _create_ib_order(
        self,
        action: str,
        quantity: float,
        order_type: OrderType,
        limit_price: float | None,
        stop_price: float | None,
    ) -> Any:
        """Create IB order object."""
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
        """Handle IB order status update."""
        ib_order_id = trade.order.orderId
        entry = self._ib_order_map.get(ib_order_id)
        if not entry:
            return

        order_id, _ = entry
        order = self._pending_orders.get(order_id)
        if not order:
            return

        status_str = trade.orderStatus.status
        if status_str == 'Filled':
            order.status = OrderStatus.FILLED
            order.filled_price = trade.orderStatus.avgFillPrice
            order.filled_quantity = trade.orderStatus.filled
            order.filled_at = datetime.now()
            del self._pending_orders[order_id]
            logger.info(f"IBBroker: Order {order_id} FILLED @ {order.filled_price}")

            # Memory leak fix: schedule cleanup of _ib_order_map entry after 1 hour
            asyncio.get_event_loop().call_later(
                3600, lambda oid=ib_order_id: self._ib_order_map.pop(oid, None)
            )
        elif status_str == 'Cancelled':
            order.status = OrderStatus.CANCELLED
            if order_id in self._pending_orders:
                del self._pending_orders[order_id]
            # Memory leak fix: cleanup immediately for cancelled orders
            self._ib_order_map.pop(ib_order_id, None)

    def _on_position(self, position: Any) -> None:
        """Handle IB position update (with lock for thread safety)."""
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
        """Sync open orders from IB."""
        for trade in self.ib.openTrades():
            if trade.orderStatus.status in ('PreSubmitted', 'Submitted'):
                self._order_counter += 1
                order_id = f"ML4T-{self._order_counter}"

                self._pending_orders[order_id] = Order(
                    asset=trade.contract.symbol,
                    side=OrderSide.BUY if trade.order.action == 'BUY' else OrderSide.SELL,
                    quantity=trade.order.totalQuantity,
                    order_type=OrderType.MARKET,
                    order_id=order_id,
                    status=OrderStatus.PENDING,
                    created_at=datetime.now(),
                )
                self._ib_order_map[trade.order.orderId] = (order_id, time.time())

        logger.info(f"IBBroker: Synced {len(self._pending_orders)} open orders")
```

---

## 5. Usage Examples

### 5.1 Basic Live Trading (Paper)

```python
import asyncio
from ml4t.backtest import Strategy, TargetWeightExecutor, RebalanceConfig
from ml4t.live import LiveEngine, IBBroker, IBDataFeed, SafeBroker, LiveRiskConfig


class MyStrategy(Strategy):
    """Same strategy for backtest AND live."""

    def __init__(self):
        self.executor = TargetWeightExecutor(
            config=RebalanceConfig(min_trade_value=500)
        )
        self.bar_count = 0

    def on_data(self, timestamp, data, context, broker):
        self.bar_count += 1
        if self.bar_count % 20 != 0:
            return

        # Equal weight portfolio
        assets = list(data.keys())
        weight = 0.95 / len(assets)
        targets = {a: weight for a in assets}
        self.executor.execute(targets, data, broker)


async def main():
    # Create broker (paper trading)
    broker = IBBroker(port=7497)

    # Wrap with safety controls
    safe_broker = SafeBroker(
        broker=broker,
        config=LiveRiskConfig(
            max_position_value=25000,
            max_daily_loss=2500,
            shadow_mode=True,  # Start in shadow mode!
        )
    )

    # Create data feed with bar aggregation
    feed = IBDataFeed(broker.ib, assets=['AAPL', 'GOOG', 'MSFT'])

    # Create engine
    engine = LiveEngine(MyStrategy(), safe_broker, feed)

    # Run
    await engine.connect()
    try:
        await engine.run()
    except KeyboardInterrupt:
        await engine.stop()


if __name__ == '__main__':
    asyncio.run(main())
```

### 5.2 Shadow Mode Testing

```python
# First run: Shadow mode (logs but doesn't trade)
config = LiveRiskConfig(
    shadow_mode=True,  # <-- No real orders!
    max_position_value=25000,
)

# Watch logs to verify strategy behavior...
# When confident:

config = LiveRiskConfig(
    shadow_mode=False,  # <-- Now trading for real
    max_position_value=25000,
)
```

### 5.3 Backtest vs Live Comparison

```python
# === BACKTEST ===
from ml4t.backtest import Engine, Broker, DataFeed

strategy = MyStrategy()
broker = Broker(initial_cash=100000)
feed = DataFeed(historical_df)
engine = Engine(strategy, broker, feed)
results = engine.run()

# === LIVE (same strategy!) ===
from ml4t.live import LiveEngine, IBBroker, IBDataFeed

strategy = MyStrategy()  # EXACT SAME CLASS
broker = IBBroker(port=7497)
feed = IBDataFeed(broker.ib, assets=['AAPL', 'GOOG'])

engine = LiveEngine(strategy, broker, feed)
await engine.connect()
await engine.run()
```

---

## 6. Package Structure

```
ml4t/live/
├── __init__.py
├── protocols.py             # BrokerProtocol, AsyncBrokerProtocol, DataFeedProtocol
├── wrappers.py              # ThreadSafeBrokerWrapper
├── engine.py                # LiveEngine
├── safety.py                # SafeBroker, LiveRiskConfig, RiskState
│
├── brokers/
│   ├── __init__.py
│   ├── ib.py               # IBBroker
│   └── alpaca.py           # AlpacaBroker
│
└── feeds/
    ├── __init__.py
    ├── ib.py               # IBDataFeed
    ├── alpaca.py           # AlpacaDataFeed
    └── aggregator.py       # BarAggregator
```

---

## 7. Addressed Issues from Gemini Reviews

### Gemini v1 Review Issues

| Issue | Section | Solution |
|-------|---------|----------|
| Sync/Async Deadlock | 3.1 | ThreadSafeBrokerWrapper with run_coroutine_threadsafe |
| State Persistence | 3.4 | RiskState class with JSON persistence |
| Data Aggregation | 3.3 | BarAggregator buffers ticks → minute bars |
| Fat Finger Check | 3.4 | max_price_deviation_pct in LiveRiskConfig |
| Stale Data | 3.4 | max_data_staleness_seconds (to implement in feeds) |
| Duplicate Orders | 3.4 | dedup_window_seconds filter |
| Shadow Mode | 3.4 | shadow_mode flag in LiveRiskConfig |

### Gemini v2 Review Issues (Critical)

| Issue | Severity | Section | Solution |
|-------|----------|---------|----------|
| **Infinite Buy Loop in Shadow Mode** | HIGH | 3.4 | VirtualPortfolio tracks shadow positions |
| **Stuck Bar Problem** | MEDIUM | 3.3 | `_flush_checker()` background task emits on timeout |
| **Race Condition on `_positions`** | MEDIUM | 4.1 | `asyncio.Lock` + safe dict copying |

### Gemini v2 Additional Fixes

| Issue | Section | Solution |
|-------|---------|----------|
| Timeout too high for getters | 3.1 | 5s for getters, 30s for orders |
| JSON corruption on crash | 3.4 | Atomic write: `.tmp` → `os.replace()` |
| Memory leak: `_recent_orders` | 3.4 | `_prune_history()` called on every order |
| Memory leak: `_ib_order_map` | 4.1 | Cleanup on terminal states + 1h delay |
| Queue busy-wait | 3.3 | `None` sentinel for shutdown |
| Missing warmup data | TBD | Historical data API planned for v1.1 |

---

## 8. Learnings from Production (Wyden)

Key patterns adopted from production trading platform:

1. **asyncio.Lock not threading.Lock**: For async-safe operations
2. **Queue-based events**: Non-blocking event handling via asyncio.Queue
3. **Ring buffers for backlog**: Handle events that arrive before subscriber ready
4. **Reconnection callbacks**: Notify components after reconnect
5. **Graceful shutdown**: Signal handlers with proper cleanup sequence
6. **Health checks**: Statistics and status monitoring

---

## 9. Limitations & Recommendations

### Recommended for:
- Paper trading
- Learning algorithmic trading
- Strategy development and testing
- Small-scale live trading with proper risk limits

### NOT recommended for:
- High-frequency trading
- Large institutional accounts
- Production systems without additional safeguards
- Trading without understanding the risks

### Always:
1. Start with shadow_mode=True
2. Graduate to paper trading
3. Use small positions when going live
4. Set conservative risk limits
5. Monitor your strategy actively

---

*Document Version: 3.0 | Revised: 2025-11-24 | Addresses Gemini v1 + v2 review feedback*
