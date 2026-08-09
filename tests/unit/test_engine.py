"""Tests for LiveEngine - async orchestration layer."""

import asyncio
import threading
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from ml4t.backtest import BacktestConfig, Strategy
from ml4t.backtest.types import Order, OrderSide, OrderStatus, OrderType, Position
from ml4t.specs import (
    BarPayload,
    EventCompletion,
    ExecutionCapability,
    FundingPayload,
    GapEvidence,
    HistoricalStrategyCompatibilityError,
    LifecyclePhase,
    LifecycleVersion,
    MarketEvent,
    MarketEventKind,
    QuotePayload,
    TradePayload,
    UnsupportedLifecycleVersionError,
)

from ml4t.live.engine import LiveEngine
from ml4t.live.feeds.events import FeedContinuityError
from ml4t.live.feeds.queue import BoundedEventQueue, FeedOverflowError
from ml4t.live.orders import CanonicalOrderRequest
from ml4t.live.protocols import AsyncBrokerProtocol, DataFeedProtocol, FeedItem
from ml4t.live.safety import LiveRiskConfig, SafeBroker

# === Mock Implementations ===


class MockAsyncBroker:
    """Mock async broker for testing."""

    def __init__(self):
        self._positions: dict[str, Position] = {}
        self._pending_orders: list[Order] = []
        self._connected = False
        self._cash = 100_000.0
        self._account_value = 100_000.0
        self.submit_calls = 0
        self.runtime_events: list[tuple[str, dict[str, Any]]] = []
        self.market_data_calls: list[tuple[datetime, dict, dict]] = []

    def record_event(self, event: str, **payload: Any) -> None:
        self.runtime_events.append((event, payload))

    def _record_market_data(self, timestamp: datetime, data: dict, context: dict) -> None:
        self.market_data_calls.append((timestamp, data, context))

    def assert_paper_trading(self) -> None:
        """Identify this deterministic adapter as a paper venue."""

    def assert_live_trading(self) -> None:
        """Allow tests that explicitly exercise the live routing contract."""

    # Properties (expected by ThreadSafeBrokerWrapper)
    @property
    def positions(self) -> dict[str, Position]:
        return self._positions.copy()

    @property
    def pending_orders(self) -> list[Order]:
        return self._pending_orders.copy()

    @property
    def is_connected(self) -> bool:
        """Sync property for direct access."""
        return self._connected

    @property
    def execution_capabilities(self) -> frozenset[ExecutionCapability]:
        return frozenset()

    # AsyncBrokerProtocol methods
    async def connect(self) -> None:
        """Connect to broker."""
        await asyncio.sleep(0.01)  # Simulate network I/O
        self._connected = True

    async def disconnect(self) -> None:
        """Disconnect from broker."""
        await asyncio.sleep(0.01)
        self._connected = False

    async def is_connected_async(self) -> bool:
        """Check if connected (async version)."""
        return self._connected

    async def get_positions_async(self) -> dict[str, Position]:
        """Get all positions (async version)."""
        return self._positions.copy()

    async def get_pending_orders_async(self, asset: str | None = None) -> list[Order]:
        """Get pending orders (async version)."""
        orders = self._pending_orders.copy()
        if asset is None:
            return orders
        return [order for order in orders if order.asset == asset]

    async def get_position_async(self, asset: str) -> Position | None:
        """Get position (async version)."""
        return self._positions.get(asset)

    async def get_cash_async(self) -> float:
        return self._cash

    async def get_account_value_async(self) -> float:
        return self._account_value

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
        """Submit order."""
        await asyncio.sleep(0.01)  # Simulate network I/O
        self.submit_calls += 1

        request = CanonicalOrderRequest.from_input(
            asset, quantity, side, order_type, limit_price, stop_price
        )
        order = Order(
            order_id=f"order_{len(self._pending_orders) + 1}",
            asset=request.asset,
            quantity=request.quantity,
            side=request.side,
            order_type=request.order_type,
            limit_price=request.limit_price,
            stop_price=request.stop_price,
            status=OrderStatus.PENDING,
            created_at=datetime.now(UTC),
        )
        self._pending_orders.append(order)
        return order

    async def cancel_order_async(self, order_id: str) -> bool:
        """Cancel order."""
        await asyncio.sleep(0.01)
        for order in self._pending_orders:
            if order.order_id == order_id:
                self._pending_orders.remove(order)
                return True
        return False

    async def replace_order_async(
        self,
        order_id: str,
        *,
        quantity: float | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> Order:
        """Replace order."""
        await asyncio.sleep(0.01)
        for order in list(self._pending_orders):
            if order.order_id == order_id:
                self._pending_orders.remove(order)
                return await self.submit_order_async(
                    asset=order.asset,
                    quantity=order.quantity if quantity is None else quantity,
                    side=order.side,
                    order_type=order.order_type,
                    limit_price=order.limit_price if limit_price is None else limit_price,
                    stop_price=order.stop_price if stop_price is None else stop_price,
                )
        raise RuntimeError(f"Order {order_id} not found")

    async def close_position_async(self, asset: str) -> Order | None:
        """Close position."""
        pos = await self.get_position_async(asset)
        if pos is None:
            return None

        side = OrderSide.SELL if pos.quantity > 0 else OrderSide.BUY
        return await self.submit_order_async(asset, abs(pos.quantity), side, OrderType.MARKET)


# Check protocol compliance
assert isinstance(MockAsyncBroker(), AsyncBrokerProtocol)


class MockDataFeed:
    """Mock data feed for testing."""

    def __init__(self, bars: list[FeedItem], delay: float = 0.01):
        self.bars = bars
        self.delay = delay
        self._started = False
        self._stopped = False

    async def start(self) -> None:
        """Start feed."""
        await asyncio.sleep(0.01)
        self._started = True
        self._stopped = False

    def stop(self) -> None:
        """Stop feed."""
        self._stopped = True

    def __aiter__(self) -> AsyncIterator[FeedItem]:
        """Return async iterator."""
        return self

    async def __anext__(self) -> FeedItem:
        """Get next bar."""
        if not self.bars or self._stopped:
            raise StopAsyncIteration

        item = self.bars.pop(0)
        await asyncio.sleep(self.delay)
        return item


class CloseableDataFeed(MockDataFeed):
    """Feed fixture with an async transport release hook."""

    def __init__(self, bars: list[FeedItem]) -> None:
        super().__init__(bars)
        self.close_count = 0

    async def close(self) -> None:
        self.close_count += 1


class OverflowDataFeed:
    """Supported-style feed that overflows before the engine can consume pending data."""

    def __init__(self, first: MarketEvent, rejected: MarketEvent) -> None:
        self.first = first
        self.rejected = rejected
        self._queue = BoundedEventQueue(capacity=1, feed="fixture")
        self._overflowed = False

    async def start(self) -> None:
        self._queue = BoundedEventQueue(capacity=1, feed="fixture")
        self._queue.put_nowait(self.first)
        self._overflowed = False

    def stop(self) -> None:
        self._queue.finish(discard=True)

    def __aiter__(self) -> AsyncIterator[MarketEvent]:
        return self

    async def __anext__(self) -> MarketEvent:
        if not self._overflowed:
            self._overflowed = True
            self._queue.put_nowait(self.rejected)
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        return item

    @property
    def stats(self) -> dict[str, Any]:
        return {"queue": self._queue.snapshot().to_dict()}


class RecoverableFeed:
    """Mock feed that can be stopped and started again by the engine watchdog."""

    def __init__(self, batches: list[list[FeedItem]], delay: float = 0.01):
        self.batches = batches
        self.delay = delay
        self._running = False
        self._queue: asyncio.Queue = asyncio.Queue()
        self._producer_task: asyncio.Task | None = None
        self.start_count = 0
        self.stop_count = 0

    async def start(self) -> None:
        self._running = True
        self._queue = asyncio.Queue()
        batch_index = min(self.start_count, len(self.batches) - 1)
        batch = self.batches[batch_index] if self.batches else []
        self.start_count += 1

        async def producer() -> None:
            for item in batch:
                if not self._running:
                    return
                await asyncio.sleep(self.delay)
                await self._queue.put(item)

        self._producer_task = asyncio.create_task(producer())

    def stop(self) -> None:
        self.stop_count += 1
        self._running = False
        if self._producer_task and not self._producer_task.done():
            self._producer_task.cancel()
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

    def __aiter__(self) -> AsyncIterator[FeedItem]:
        return self

    async def __anext__(self) -> FeedItem:
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        return item


# Check protocol compliance
assert isinstance(MockDataFeed([]), DataFeedProtocol), (
    "MockDataFeed does not implement DataFeedProtocol"
)


class RecordingStrategy(Strategy):
    """Test strategy that records calls."""

    def __init__(self):
        self.on_start_called = False
        self.on_data_calls: list[tuple[datetime, dict, dict]] = []
        self.on_end_called = False
        self.broker_ref = None

    def on_start(self, broker: Any) -> None:
        self.on_start_called = True
        self.broker_ref = broker

    def on_data(self, timestamp: datetime, data: dict, context: dict, broker: Any) -> None:
        self.on_data_calls.append((timestamp, data, context))
        self.broker_ref = broker

    def on_end(self, broker: Any) -> None:
        self.on_end_called = True


class LifecycleStrategy(Strategy):
    """Record callback order, thread identity, and portable broker access."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, float]] = []
        self.prepare_config: BacktestConfig | None = None

    def _record(self, callback: str, broker: Any) -> None:
        assert broker.is_connected is True
        assert broker.get_position("AAPL") is not None
        assert "AAPL" in broker.positions
        assert broker.get_account_value() == 100_000.0
        assert broker.get_pending_orders() == []
        order = broker.submit_order("MSFT", 1)
        replacement = broker.replace_order(order.order_id, quantity=2)
        assert broker.cancel_order(replacement.order_id) is True
        closing = broker.close_position("AAPL")
        assert closing is not None
        assert broker.cancel_order(closing.order_id) is True
        assert broker.pending_orders == []
        self.calls.append((callback, threading.get_ident(), broker.get_cash()))

    def on_start(self, broker: Any) -> None:
        self._record("on_start", broker)

    def on_prepare(self, broker: Any, config: Any | None = None) -> None:
        self.prepare_config = config
        self._record("on_prepare", broker)

    def on_data(self, timestamp: datetime, data: dict, context: dict, broker: Any) -> None:
        self._record("on_data", broker)

    def on_end(self, broker: Any) -> None:
        self._record("on_end", broker)


class ErrorStrategy(Strategy):
    """Strategy that raises exceptions."""

    def __init__(self, error_on_bar: int = 0):
        self.error_on_bar = error_on_bar
        self.call_count = 0

    def on_data(self, timestamp: datetime, data: dict, context: dict, broker: Any) -> None:
        self.call_count += 1
        if self.call_count == self.error_on_bar:
            raise ValueError("Test error")


class ShadowEntryStrategy(Strategy):
    """Strategy that enters once and relies on position state thereafter."""

    def __init__(self):
        self.order_attempts = 0

    def on_data(self, timestamp: datetime, data: dict, context: dict, broker: Any) -> None:
        if broker.get_position("AAPL") is None:
            self.order_attempts += 1
            broker.submit_order("AAPL", 10, side=OrderSide.BUY)


def typed_trade(
    timestamp: datetime,
    sequence: int | None,
    *,
    gap: GapEvidence | None = None,
) -> MarketEvent:
    return MarketEvent(
        version=LifecycleVersion.V1,
        event_time=timestamp,
        receipt_time=timestamp,
        kind=MarketEventKind.TRADE,
        completion=EventCompletion.COMPLETE,
        source="fixture",
        asset="AAPL",
        payload=TradePayload(150.0, 1.0),
        provider_sequence=sequence,
        gap=gap,
    )


# === Test Cases ===


@pytest.mark.asyncio
async def test_engine_initialization():
    """Test LiveEngine initialization."""
    strategy = RecordingStrategy()
    broker = MockAsyncBroker()
    feed = MockDataFeed([])

    engine = LiveEngine(strategy, broker, feed)

    assert engine.strategy is strategy
    assert engine.broker is broker
    assert engine.feed is feed
    assert engine._running is False
    assert engine._wrapped_broker is None


@pytest.mark.asyncio
async def test_connect():
    """Test broker and feed connection."""
    strategy = RecordingStrategy()
    broker = MockAsyncBroker()
    feed = MockDataFeed([])

    engine = LiveEngine(strategy, broker, feed)
    await engine.connect()

    # Broker connected
    assert broker.is_connected is True

    # Feed started
    assert feed._started is True

    # Wrapper created
    assert engine._wrapped_broker is not None
    assert engine._loop is not None


@pytest.mark.asyncio
async def test_run_empty_feed():
    """Test engine with empty data feed."""
    strategy = RecordingStrategy()
    broker = MockAsyncBroker()
    feed = MockDataFeed([])

    engine = LiveEngine(strategy, broker, feed)
    await engine.connect()
    await engine.run()

    # Lifecycle callbacks called
    assert strategy.on_start_called is True
    assert strategy.on_end_called is True

    # No data received
    assert len(strategy.on_data_calls) == 0
    assert engine.stats["bar_count"] == 0


@pytest.mark.asyncio
async def test_run_with_data():
    """Test engine processes bars correctly."""
    strategy = RecordingStrategy()
    broker = MockAsyncBroker()

    # Create test bars
    bars = [
        (
            datetime(2024, 1, 1, 9, 30, tzinfo=UTC),
            {"AAPL": {"open": 150.0, "high": 151.0, "low": 149.0, "close": 150.5}},
            {"bar_type": "1min"},
        ),
        (
            datetime(2024, 1, 1, 9, 31, tzinfo=UTC),
            {"AAPL": {"open": 150.5, "high": 151.5, "low": 150.0, "close": 151.0}},
            {"bar_type": "1min"},
        ),
        (
            datetime(2024, 1, 1, 9, 32, tzinfo=UTC),
            {"AAPL": {"open": 151.0, "high": 152.0, "low": 151.0, "close": 151.5}},
            {"bar_type": "1min"},
        ),
    ]
    feed = MockDataFeed(bars)

    engine = LiveEngine(strategy, broker, feed)
    await engine.connect()
    await engine.run()

    # All bars processed
    assert len(strategy.on_data_calls) == 3
    assert strategy.on_data_calls[0][0] == datetime(2024, 1, 1, 9, 30, tzinfo=UTC)
    assert strategy.on_data_calls[1][0] == datetime(2024, 1, 1, 9, 31, tzinfo=UTC)
    assert strategy.on_data_calls[2][0] == datetime(2024, 1, 1, 9, 32, tzinfo=UTC)

    # Stats updated
    assert engine.stats["bar_count"] == 3
    assert engine.stats["error_count"] == 0
    assert engine.stats["last_bar_time"] == datetime(2024, 1, 1, 9, 32, tzinfo=UTC)


@pytest.mark.asyncio
async def test_typed_events_dispatch_by_kind_and_preserve_causal_context() -> None:
    strategy = RecordingStrategy()
    broker = MockAsyncBroker()
    now = datetime.now(UTC)
    event_parameters = [
        (MarketEventKind.BAR, EventCompletion.COMPLETE, BarPayload(149, 151, 148, 150, 100)),
        (MarketEventKind.QUOTE, EventCompletion.EVOLVING, QuotePayload(149, 151, 10, 20)),
        (MarketEventKind.TRADE, EventCompletion.COMPLETE, TradePayload(150, 5)),
        (MarketEventKind.FUNDING, EventCompletion.COMPLETE, FundingPayload(0.0001)),
        (MarketEventKind.BAR, EventCompletion.EVOLVING, BarPayload(150, 152, 149, 151, 50)),
    ]
    events = [
        MarketEvent(
            version=LifecycleVersion.V1,
            event_time=now + timedelta(microseconds=index),
            receipt_time=now + timedelta(microseconds=index),
            kind=kind,
            completion=completion,
            source="fixture",
            asset="AAPL",
            payload=payload,
            provider_sequence=index,
            metadata={"fixture_index": index},
        )
        for index, (kind, completion, payload) in enumerate(event_parameters, start=1)
    ]
    engine = LiveEngine(strategy, broker, MockDataFeed(events))

    await engine.connect()
    await engine.run()

    assert len(strategy.on_data_calls) == 5
    assert [call[2]["_market_event"]["kind"] for call in strategy.on_data_calls] == [
        "bar",
        "quote",
        "trade",
        "funding",
        "bar",
    ]
    assert strategy.on_data_calls[1][1]["AAPL"]["price"] == 150.0
    assert strategy.on_data_calls[3][1] == {"AAPL": {"funding_rate": 0.0001}}
    assert strategy.on_data_calls[-1][2]["_market_event"]["completion"] == "evolving"
    assert len(broker.market_data_calls) == 4
    assert engine.stats["event_count"] == 5
    assert engine.stats["bar_count"] == 2
    assert engine.stats["event_kind_counts"] == {
        "bar": 2,
        "trade": 1,
        "quote": 1,
        "funding": 1,
    }


@pytest.mark.asyncio
async def test_stale_typed_event_cannot_reach_strategy_or_broker_state() -> None:
    strategy = RecordingStrategy()
    broker = MockAsyncBroker()
    old = datetime.now(UTC) - timedelta(minutes=5)
    event = MarketEvent(
        version=LifecycleVersion.V1,
        event_time=old,
        receipt_time=old,
        kind=MarketEventKind.TRADE,
        completion=EventCompletion.COMPLETE,
        source="fixture",
        asset="AAPL",
        payload=TradePayload(150, 5),
        gap=GapEvidence(False, "fixture sequence unavailable"),
    )
    engine = LiveEngine(
        strategy,
        broker,
        MockDataFeed([event]),
        max_event_age_seconds=1.0,
    )

    await engine.connect()
    with pytest.raises(ValueError, match="event is stale"):
        await engine.run()

    assert strategy.on_data_calls == []
    assert broker.market_data_calls == []
    assert engine.stats["event_count"] == 0


@pytest.mark.asyncio
async def test_exact_provider_replay_is_skipped_before_callback() -> None:
    strategy = RecordingStrategy()
    broker = MockAsyncBroker()
    event = typed_trade(datetime.now(UTC), 1)
    engine = LiveEngine(strategy, broker, MockDataFeed([event, event]))

    await engine.connect()
    await engine.run()

    assert len(strategy.on_data_calls) == 1
    assert len(broker.market_data_calls) == 1
    assert engine.stats["event_count"] == 1
    assert engine.stats["continuity"]["duplicate_count"] == 1
    assert [
        operational["event"]
        for operational in engine.operational_events
        if operational["event"] == "feed_duplicate_skipped"
    ] == ["feed_duplicate_skipped"]


@pytest.mark.asyncio
async def test_explicit_provider_gap_halts_before_callback_and_records_evidence() -> None:
    strategy = RecordingStrategy()
    broker = MockAsyncBroker()
    event = typed_trade(
        datetime.now(UTC),
        3,
        gap=GapEvidence(
            True,
            "provider sequence gap",
            previous_sequence=1,
            current_sequence=3,
        ),
    )
    engine = LiveEngine(strategy, broker, MockDataFeed([event]))

    await engine.connect()
    with pytest.raises(FeedContinuityError, match="provider sequence gap"):
        await engine.run()

    assert strategy.on_data_calls == []
    assert broker.market_data_calls == []
    safety_events = [
        payload for name, payload in broker.runtime_events if name == "feed_safety_halt"
    ]
    assert safety_events[-1]["detail"]["reason"] == "provider sequence gap"
    assert engine.stats["continuity"]["violation_count"] == 1


@pytest.mark.asyncio
async def test_queue_overflow_halts_before_pending_callback_and_records_gap() -> None:
    strategy = RecordingStrategy()
    broker = MockAsyncBroker()
    now = datetime.now(UTC)
    feed = OverflowDataFeed(
        typed_trade(now, 1),
        typed_trade(now + timedelta(microseconds=1), 2),
    )
    engine = LiveEngine(strategy, broker, feed)

    await engine.connect()
    with pytest.raises(FeedOverflowError, match="capacity 1 exceeded"):
        await engine.run()

    assert strategy.on_data_calls == []
    assert broker.market_data_calls == []
    safety_events = [
        payload for name, payload in broker.runtime_events if name == "feed_safety_halt"
    ]
    assert safety_events[-1]["detail"]["gap"]["detected"] is True
    assert safety_events[-1]["detail"]["gap"]["current_sequence"] == 2
    assert engine.stats["feed"]["queue"]["overflow_count"] == 1
    assert [transition.current.value for transition in engine.runtime_transitions][-3:] == [
        "degraded",
        "stopping",
        "failed",
    ]


@pytest.mark.asyncio
async def test_shared_lifecycle_dispatches_all_callbacks_on_one_worker_thread() -> None:
    strategy = LifecycleStrategy()
    broker = MockAsyncBroker()
    broker._positions["AAPL"] = Position(
        asset="AAPL",
        quantity=1,
        entry_price=100.0,
        entry_time=datetime(2024, 1, 1, tzinfo=UTC),
        current_price=100.0,
    )
    feed = MockDataFeed([(datetime(2024, 1, 1, 9, 30, tzinfo=UTC), {"AAPL": {"close": 150.0}}, {})])
    loop_thread = threading.get_ident()
    engine = LiveEngine(strategy, broker, feed)

    await engine.connect()
    await engine.run()

    assert [callback for callback, _, _ in strategy.calls] == [
        "on_start",
        "on_prepare",
        "on_data",
        "on_end",
    ]
    callback_threads = {thread_id for _, thread_id, _ in strategy.calls}
    assert len(callback_threads) == 1
    assert loop_thread not in callback_threads
    assert all(cash == 100_000.0 for _, _, cash in strategy.calls)
    assert [entry.phase for entry in engine.lifecycle_dispatcher.invocations] == [
        LifecyclePhase.RUN_START,
        LifecyclePhase.CAUSAL_INITIALIZATION,
        LifecyclePhase.MARKET_EVENT,
        LifecyclePhase.RUN_END,
    ]
    assert engine.stats["lifecycle_version"] == "1"
    assert strategy.prepare_config is engine.strategy_config
    assert isinstance(strategy.prepare_config, BacktestConfig)
    assert engine.stats["callback_counts"] == {
        "run_start": 1,
        "causal_initialization": 1,
        "pre_open": 0,
        "opening_auction": 0,
        "fill_reconciliation": 0,
        "intrabar": 0,
        "close": 0,
        "market_event": 1,
        "run_end": 1,
    }
    assert [event for event, _ in broker.runtime_events if event.startswith("strategy_")] == []
    assert [
        event["event"]
        for event in engine.operational_events
        if event["event"].startswith("strategy_callback_")
    ] == [
        "strategy_callback_started",
        "strategy_callback_succeeded",
        "strategy_callback_started",
        "strategy_callback_succeeded",
        "strategy_callback_started",
        "strategy_callback_succeeded",
        "strategy_callback_started",
        "strategy_callback_succeeded",
    ]


def test_unsupported_lifecycle_rejected_before_runtime_side_effects() -> None:
    broker = MockAsyncBroker()
    feed = MockDataFeed([])

    with pytest.raises(UnsupportedLifecycleVersionError, match="unsupported"):
        LiveEngine(RecordingStrategy(), broker, feed, lifecycle_version="unsupported")

    assert broker.is_connected is False
    assert feed._started is False


def test_historical_strategy_rejected_before_runtime_side_effects() -> None:
    class HistoricalStrategy(RecordingStrategy):
        def on_before_risk(self, timestamp, data, context, broker) -> None:
            return None

    broker = MockAsyncBroker()
    feed = MockDataFeed([])

    with pytest.raises(HistoricalStrategyCompatibilityError, match="on_before_risk"):
        LiveEngine(HistoricalStrategy(), broker, feed)

    assert broker.is_connected is False
    assert feed._started is False


@pytest.mark.asyncio
async def test_strategy_receives_wrapper():
    """Test strategy receives ThreadSafeBrokerWrapper, not raw broker."""
    strategy = RecordingStrategy()
    broker = MockAsyncBroker()
    bars = [
        (
            datetime(2024, 1, 1, 9, 30, tzinfo=UTC),
            {"AAPL": {"close": 150.0}},
            {},
        )
    ]
    feed = MockDataFeed(bars)

    engine = LiveEngine(strategy, broker, feed)
    await engine.connect()
    await engine.run()

    # Strategy received wrapper
    assert strategy.broker_ref is not None
    assert strategy.broker_ref is engine._wrapped_broker
    assert strategy.broker_ref is not broker


@pytest.mark.asyncio
async def test_graceful_shutdown_via_stop():
    """Test engine stops gracefully when stop() called."""
    strategy = RecordingStrategy()
    broker = MockAsyncBroker()

    # Long-running feed (100 minutes worth of bars)
    bars = [
        (
            datetime(2024, 1, 1, 9 + i // 60, i % 60, tzinfo=UTC),
            {"AAPL": {"close": 150.0}},
            {},
        )
        for i in range(30, 130)  # 9:30 to 11:10
    ]
    feed = MockDataFeed(bars, delay=0.05)

    engine = LiveEngine(strategy, broker, feed)
    await engine.connect()

    # Start engine and stop after 0.1s
    async def stop_soon():
        await asyncio.sleep(0.15)  # Let a few bars process
        await engine.stop()

    await asyncio.gather(engine.run(), stop_soon())

    # Engine stopped early (not all 100 bars processed)
    assert strategy.on_data_calls  # Some bars processed
    assert len(strategy.on_data_calls) < 100  # But not all
    assert strategy.on_end_called is True


@pytest.mark.asyncio
async def test_strategy_error_aborts_and_runs_cleanup_once():
    """Test shared rollback-and-abort semantics for a strategy error."""
    strategy = ErrorStrategy(error_on_bar=2)  # Raise error on 2nd bar
    broker = MockAsyncBroker()
    bars = [
        (datetime(2024, 1, 1, 9, 30, tzinfo=UTC), {"AAPL": {"close": 150.0}}, {}),
        (datetime(2024, 1, 1, 9, 31, tzinfo=UTC), {"AAPL": {"close": 151.0}}, {}),
        (datetime(2024, 1, 1, 9, 32, tzinfo=UTC), {"AAPL": {"close": 152.0}}, {}),
    ]
    feed = MockDataFeed(bars)

    errors: list[Exception] = []

    def error_handler(e: Exception, timestamp: datetime, data: dict) -> None:
        errors.append(e)

    engine = LiveEngine(strategy, broker, feed, on_error=error_handler)
    await engine.connect()
    with pytest.raises(ValueError, match="Test error"):
        await engine.run()

    assert strategy.call_count == 2

    # Error captured
    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)
    assert str(errors[0]) == "Test error"

    # Stats updated
    assert engine.stats["bar_count"] == 2
    assert engine.stats["error_count"] == 1
    assert engine.lifecycle_dispatcher.callback_counts[LifecyclePhase.RUN_END] == 1
    strategy_events = [
        event for event, _ in broker.runtime_events if event.startswith("strategy_callback_")
    ]
    assert strategy_events == ["strategy_callback_failed"]
    assert [
        event["event"]
        for event in engine.operational_events
        if event["event"].startswith("strategy_callback_")
    ][-3:] == [
        "strategy_callback_failed",
        "strategy_callback_started",
        "strategy_callback_succeeded",
    ]


@pytest.mark.asyncio
async def test_external_cancellation_runs_cleanup_once_and_propagates() -> None:
    strategy = RecordingStrategy()
    broker = MockAsyncBroker()
    feed = RecoverableFeed([[]])
    engine = LiveEngine(strategy, broker, feed)
    await engine.connect()
    run_task = asyncio.create_task(engine.run())

    while not any(
        event["event"] == "strategy_callback_succeeded"
        and event.get("phase") == LifecyclePhase.CAUSAL_INITIALIZATION.value
        for event in engine.operational_events
    ):
        await asyncio.sleep(0)
    run_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await run_task

    assert strategy.on_end_called is True
    assert engine.lifecycle_dispatcher.callback_counts[LifecyclePhase.RUN_END] == 1
    assert engine._watchdog_task is None
    assert broker.is_connected is False
    assert feed.stop_count == 1
    assert engine.runtime_state.value == "stopped"
    assert [
        event["event"]
        for event in engine.operational_events
        if event["event"].startswith("strategy_callback_")
    ][-2:] == [
        "strategy_callback_started",
        "strategy_callback_succeeded",
    ]
    assert not any(event.startswith("strategy_callback_") for event, _ in broker.runtime_events)


@pytest.mark.asyncio
async def test_repeated_runs_leave_no_lifecycle_threads_or_watchdog_tasks() -> None:
    for _ in range(5):
        engine = LiveEngine(RecordingStrategy(), MockAsyncBroker(), MockDataFeed([]))
        await engine.connect()
        await engine.run()

        assert engine._watchdog_task is None
        assert not any(
            task.get_name() == "ml4t-live-watchdog"
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
        )

    assert not any(thread.name.startswith("ml4t-live-strategy") for thread in threading.enumerate())


@pytest.mark.asyncio
async def test_runtime_closes_feed_transport_after_stop() -> None:
    feed = CloseableDataFeed([])
    engine = LiveEngine(RecordingStrategy(), MockAsyncBroker(), feed)

    await engine.connect()
    await engine.run()

    assert feed.close_count == 1
    assert engine.stats["last_cleanup_result"]["feed_close"] == "released"


@pytest.mark.asyncio
async def test_stats_property():
    """Test stats property returns correct info."""
    strategy = RecordingStrategy()
    broker = MockAsyncBroker()
    bars = [
        (datetime(2024, 1, 1, 9, 30, tzinfo=UTC), {"AAPL": {"close": 150.0}}, {}),
        (datetime(2024, 1, 1, 9, 31, tzinfo=UTC), {"AAPL": {"close": 151.0}}, {}),
    ]
    feed = MockDataFeed(bars)

    engine = LiveEngine(strategy, broker, feed)
    await engine.connect()

    # Before run
    stats = engine.stats
    assert stats["running"] is False
    assert stats["bar_count"] == 0
    assert stats["error_count"] == 0
    assert stats["last_bar_time"] is None

    await engine.run()

    # After run
    stats = engine.stats
    assert stats["running"] is False
    assert stats["bar_count"] == 2
    assert stats["error_count"] == 0
    assert stats["last_bar_time"] == datetime(2024, 1, 1, 9, 31, tzinfo=UTC)


def test_runtime_status_reports_feed_silence_for_open_equity_session():
    """Test runtime_status flags open-session feed silence."""
    strategy = RecordingStrategy()
    broker = MockAsyncBroker()
    broker._connected = True
    feed = MockDataFeed([])
    feed._stock_symbols = ["AAPL"]

    engine = LiveEngine(strategy, broker, feed, feed_silence_seconds=30.0)
    engine._running = True
    now = datetime(2024, 1, 2, 15, 0, tzinfo=UTC)
    engine._last_bar_received_at = now - timedelta(seconds=45)

    status = engine.runtime_status(now=now)

    assert status["session_state"] == "open"
    assert status["health"] == "feed_silent"
    assert status["last_bar_age_seconds"] == 45.0


@pytest.mark.asyncio
async def test_watchdog_auto_recovers_after_feed_silence():
    """Test watchdog restarts broker/feed after a feed-silent event."""
    strategy = RecordingStrategy()
    broker = MockAsyncBroker()
    feed = RecoverableFeed(
        [
            [
                (
                    datetime(2024, 1, 1, 9, 30, tzinfo=UTC),
                    {"AAPL": {"close": 150.0}},
                    {},
                )
            ],
            [
                (
                    datetime(2024, 1, 1, 9, 31, tzinfo=UTC),
                    {"AAPL": {"close": 151.0}},
                    {},
                )
            ],
        ],
        delay=0.01,
    )
    health_events: list[str] = []

    engine = LiveEngine(
        strategy,
        broker,
        feed,
        feed_silence_seconds=0.05,
        watchdog_poll_seconds=0.01,
        auto_recover=True,
        recovery_cooldown_seconds=0.01,
        max_recovery_attempts=1,
        on_health_change=lambda health, status: health_events.append(health),
    )
    await engine.connect()

    async def stop_after_recovery() -> None:
        while len(strategy.on_data_calls) < 2:
            await asyncio.sleep(0.01)
        await engine.stop()

    await asyncio.wait_for(asyncio.gather(engine.run(), stop_after_recovery()), timeout=1.0)

    assert len(strategy.on_data_calls) >= 2
    assert feed.start_count >= 2
    assert engine.stats["recovery_attempts"] == 1
    assert engine.runtime_state.value == "stopped"
    assert engine.stats["callback_counts"]["run_start"] == 1
    assert engine.stats["callback_counts"]["causal_initialization"] == 1
    assert engine.stats["callback_counts"]["run_end"] == 1
    recovery_events = [
        event
        for event in engine.operational_events
        if event["event"] == "engine_recovery_succeeded"
    ]
    assert len(recovery_events) == 1
    assert recovery_events[0]["attempt"] == 1
    assert recovery_events[0]["duration_seconds"] >= 0
    assert recovery_events[0]["last_known_sequence"] >= 1
    assert recovery_events[0]["terminal_status"] == "running"
    assert recovery_events[0]["cleanup_result"] == {
        "feed": "not_acquired",
        "broker": "released",
    }
    assert "feed_silent" in health_events


@pytest.mark.asyncio
async def test_recovery_skips_replay_then_accepts_monotonic_provider_sequence() -> None:
    strategy = RecordingStrategy()
    broker = MockAsyncBroker()
    now = datetime.now(UTC)
    first = typed_trade(now, 10)
    second = typed_trade(
        now + timedelta(milliseconds=1),
        11,
        gap=GapEvidence(
            False,
            "fixture continuity proved",
            previous_sequence=10,
            current_sequence=11,
        ),
    )
    feed = RecoverableFeed([[first], [first, second]], delay=0.01)
    engine = LiveEngine(
        strategy,
        broker,
        feed,
        feed_silence_seconds=0.05,
        watchdog_poll_seconds=0.01,
        auto_recover=True,
        recovery_cooldown_seconds=0.01,
        max_recovery_attempts=1,
    )
    await engine.connect()

    async def stop_after_continuation() -> None:
        while len(strategy.on_data_calls) < 2:
            await asyncio.sleep(0.01)
        await engine.stop()

    await asyncio.wait_for(
        asyncio.gather(engine.run(), stop_after_continuation()),
        timeout=1.0,
    )

    assert len(strategy.on_data_calls) == 2
    assert engine.stats["continuity"]["generation"] == 1
    assert engine.stats["continuity"]["duplicate_count"] == 1
    assert engine.stats["continuity"]["last_sequences"] == {"fixture:AAPL:trade": 11}


@pytest.mark.asyncio
async def test_recovery_without_provider_continuity_halts_before_new_decision() -> None:
    strategy = RecordingStrategy()
    broker = MockAsyncBroker()
    now = datetime.now(UTC)
    unavailable = GapEvidence(False, "fixture provider sequence unavailable")
    first = typed_trade(now, None, gap=unavailable)
    second = typed_trade(now + timedelta(milliseconds=1), None, gap=unavailable)
    feed = RecoverableFeed([[first], [second]], delay=0.01)
    engine = LiveEngine(
        strategy,
        broker,
        feed,
        feed_silence_seconds=0.05,
        watchdog_poll_seconds=0.01,
        auto_recover=True,
        recovery_cooldown_seconds=0.01,
        max_recovery_attempts=1,
    )
    await engine.connect()

    with pytest.raises(FeedContinuityError, match="unavailable after reconnect"):
        await asyncio.wait_for(engine.run(), timeout=1.0)

    assert len(strategy.on_data_calls) == 1
    assert len(broker.market_data_calls) == 1
    assert engine.stats["continuity"]["violation_count"] == 1


@pytest.mark.asyncio
async def test_watchdog_halts_when_unhealthy_without_auto_recover():
    """Test watchdog stops the engine when halt_on_unhealthy is enabled."""
    strategy = RecordingStrategy()
    broker = MockAsyncBroker()
    feed = RecoverableFeed(
        [
            [
                (
                    datetime(2024, 1, 1, 9, 30, tzinfo=UTC),
                    {"AAPL": {"close": 150.0}},
                    {},
                )
            ]
        ],
        delay=0.01,
    )

    engine = LiveEngine(
        strategy,
        broker,
        feed,
        feed_silence_seconds=0.05,
        watchdog_poll_seconds=0.01,
        halt_on_unhealthy=True,
    )
    await engine.connect()
    await asyncio.wait_for(engine.run(), timeout=1.0)

    assert len(strategy.on_data_calls) == 1
    assert feed.start_count == 1
    assert engine.stats["health"] == "stopped"


def test_runtime_status_reports_market_closed_idle_state():
    """Test runtime_status distinguishes closed markets from feed failure."""
    strategy = RecordingStrategy()
    broker = MockAsyncBroker()
    broker._connected = True
    feed = MockDataFeed([])
    feed._stock_symbols = ["AAPL"]

    engine = LiveEngine(strategy, broker, feed, feed_silence_seconds=30.0)
    engine._running = True
    now = datetime(2024, 1, 6, 18, 0, tzinfo=UTC)

    status = engine.runtime_status(now=now)

    assert status["session_state"] == "closed"
    assert status["health"] == "idle_market_closed"
    assert status["next_session_boundary"] is not None


@pytest.mark.asyncio
async def test_run_without_connect_raises():
    """Test run() raises if connect() not called first."""
    strategy = RecordingStrategy()
    broker = MockAsyncBroker()
    feed = MockDataFeed([])

    engine = LiveEngine(strategy, broker, feed)

    with pytest.raises(RuntimeError, match="Call connect\\(\\) before run\\(\\)"):
        await engine.run()


@pytest.mark.asyncio
async def test_disconnect_on_stop():
    """Test broker disconnects when engine stops."""
    strategy = RecordingStrategy()
    broker = MockAsyncBroker()
    feed = MockDataFeed([])

    engine = LiveEngine(strategy, broker, feed)
    await engine.connect()

    assert broker.is_connected is True

    await engine.run()
    await engine.stop()

    # Broker disconnected
    assert broker.is_connected is False


@pytest.mark.asyncio
async def test_feed_stops_on_engine_stop():
    """Test data feed stops when engine stops."""
    strategy = RecordingStrategy()
    broker = MockAsyncBroker()
    feed = MockDataFeed([])

    engine = LiveEngine(strategy, broker, feed)
    await engine.connect()

    assert feed._stopped is False

    await engine.stop()

    # Feed stopped
    assert feed._stopped is True


@pytest.mark.asyncio
async def test_shadow_mode_end_to_end_uses_virtual_portfolio(tmp_path):
    """Test LiveEngine + SafeBroker shadow mode without real broker orders."""
    strategy = ShadowEntryStrategy()
    broker = MockAsyncBroker()
    bars = [
        (datetime(2024, 1, 1, 9, 30, tzinfo=UTC), {"AAPL": {"close": 150.0}}, {}),
        (datetime(2024, 1, 1, 9, 31, tzinfo=UTC), {"AAPL": {"close": 151.0}}, {}),
    ]
    feed = MockDataFeed(bars)
    safe_broker = SafeBroker(
        broker,
        LiveRiskConfig(
            shadow_mode=True,
            dedup_window_seconds=0.0,
            max_position_value=50_000.0,
            max_order_value=10_000.0,
            state_file=str(tmp_path / "shadow_mode_state.json"),
        ),
    )

    engine = LiveEngine(strategy, safe_broker, feed)
    await engine.connect()
    await engine.run()

    virtual_position = safe_broker._virtual_portfolio.positions.get("AAPL")

    assert strategy.order_attempts == 1
    assert broker.submit_calls == 0
    assert virtual_position is not None
    assert virtual_position.quantity == 10
    assert virtual_position.entry_price == 150.0
    assert virtual_position.current_price == 151.0
