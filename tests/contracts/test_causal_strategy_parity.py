from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from typing import Any

import polars as pl
import pytest
from ml4t.backtest import (
    BacktestConfig,
    DataFeed,
    Engine,
    StopLoss,
    Strategy,
)
from ml4t.backtest import callback_trace as backtest_callback_trace
from ml4t.backtest.config import DataFrequency
from ml4t.backtest.execution import RebalanceSchedule
from ml4t.backtest.strategies import LongShortStrategy
from ml4t.specs import (
    AssetTarget,
    BarLabel,
    CanonicalTargetIntent,
    ExecutionBehavior,
    ExecutionPolicy,
    FeedSpec,
    HistoricalStrategyCompatibilityError,
    IntentReason,
    LifecyclePhase,
    ResidualPolicy,
    RoundingPolicy,
    TargetMeasure,
    TimestampSemantics,
)

from ml4t.live import (
    LiveEngine,
    LiveRiskConfig,
    SafeBroker,
    UnsupportedLiveCapabilityError,
    default_live_execution_policy,
)
from ml4t.live import (
    callback_trace as live_callback_trace,
)


@dataclass
class StrategyTrace:
    callbacks: list[str] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)


class ContractStrategy(Strategy):
    """One strategy source used without runtime branches by both real engines."""

    def __init__(
        self,
        trace: StrategyTrace,
        initial_target: CanonicalTargetIntent,
        scheduled_target: CanonicalTargetIntent | None = None,
    ) -> None:
        self.trace = trace
        self.initial_target = initial_target
        self.scheduled_target = scheduled_target

    def on_start(self, broker) -> None:
        self.trace.callbacks.append("on_start")

    def on_prepare(self, broker, config=None) -> None:
        self.trace.callbacks.append("on_prepare")
        rules = StopLoss(0.05) if self.initial_target.position_rule_policy_id is not None else None
        broker.register_target_intent(self.initial_target, position_rules=rules)

    def on_data(self, timestamp, data, context, broker) -> None:
        self.trace.callbacks.append("on_data")
        self.trace.observations.append(
            {
                "timestamp": _utc(timestamp).isoformat(),
                "fields": tuple(sorted(data["SPY"])),
                "position": (
                    broker.get_position("SPY").quantity
                    if broker.get_position("SPY") is not None
                    else 0.0
                ),
                "pending": tuple(
                    (order.asset, order.side.value, order.quantity, order.order_type.value)
                    for order in broker.get_pending_orders("SPY")
                ),
                "target_ids": tuple(intent.intent_id for intent in broker.get_target_intents()),
            }
        )
        if self.scheduled_target is not None and timestamp.date() == date(2026, 8, 10):
            broker.register_target_intent(self.scheduled_target)

    def on_end(self, broker) -> None:
        self.trace.callbacks.append("on_end")


class DeterministicLiveBroker:
    def __init__(self) -> None:
        self._connected = False
        self._positions = {}
        self._pending_orders = []
        self.connect_calls = 0

    @property
    def positions(self):
        return dict(self._positions)

    @property
    def pending_orders(self):
        return list(self._pending_orders)

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def execution_capabilities(self) -> frozenset[str]:
        return frozenset()

    async def connect(self) -> None:
        self.connect_calls += 1
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def is_connected_async(self) -> bool:
        return self._connected

    async def get_positions_async(self):
        return self.positions

    async def get_pending_orders_async(self, asset: str | None = None):
        if asset is None:
            return self.pending_orders
        return [order for order in self._pending_orders if order.asset == asset]

    async def get_position_async(self, asset: str):
        return self._positions.get(asset)

    def get_position(self, asset: str):
        return self._positions.get(asset)

    async def get_account_value_async(self) -> float:
        return 100_000.0

    async def get_cash_async(self) -> float:
        return 100_000.0

    async def submit_order_async(self, *args, **kwargs):
        raise AssertionError("shadow execution must not submit to the venue")

    async def cancel_order_async(self, order_id: str) -> bool:
        return False

    async def replace_order_async(self, order_id: str, **kwargs):
        raise AssertionError("contract fixture does not replace orders")

    async def close_position_async(self, asset: str):
        return None


class DeterministicLiveFeed:
    def __init__(
        self,
        events: list[tuple[datetime, dict[str, dict[str, float]], dict]],
        *,
        feed_spec: FeedSpec | None = None,
    ) -> None:
        self.feed_spec = feed_spec
        self._events = [
            (
                timestamp,
                {
                    asset: {
                        **values,
                        "price": values["close"],
                        "signals": values.get("signals", {}),
                    }
                    for asset, values in data.items()
                },
                context,
            )
            for timestamp, data, context in events
        ]
        self._running = False

    async def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._running or not self._events:
            raise StopAsyncIteration
        await asyncio.sleep(0)
        return self._events.pop(0)


@dataclass
class EngineEvidence:
    callback_trace: tuple[tuple[str, str, datetime | None], ...]
    strategy_trace: StrategyTrace
    policy: dict[str, Any]
    targets: tuple[dict[str, Any], ...]
    children: tuple[dict[str, Any], ...]
    reconciliations: tuple[dict[str, Any], ...]
    exit_reason: str | None


def target_intent(
    *,
    intent_id: str = "initial-portfolio",
    session: date = date(2026, 8, 10),
    decision_time: datetime = datetime(2026, 8, 9, 20, tzinfo=UTC),
    weight: float = 0.5,
    position_rule_policy_id: str | None = "stop-5",
) -> CanonicalTargetIntent:
    return CanonicalTargetIntent(
        intent_id=intent_id,
        decision_time=decision_time,
        information_cutoff=decision_time,
        effective_session=session,
        effective_phase=LifecyclePhase.PRE_OPEN,
        targets=(AssetTarget("SPY", TargetMeasure.WEIGHT, weight),),
        idempotency_key=f"{intent_id}-key",
        measure=TargetMeasure.WEIGHT,
        cash_buffer=0.0,
        rounding=RoundingPolicy.TOWARD_ZERO,
        residual=ResidualPolicy.KEEP_CASH,
        reason=IntentReason.REBALANCE,
        position_rule_policy_id=position_rule_policy_id,
    )


def market_events() -> list[tuple[datetime, dict[str, dict[str, float]], dict]]:
    return [
        (
            datetime(2026, 8, 10, 13, 30, tzinfo=UTC),
            {
                "SPY": {
                    "open": 100.0,
                    "high": 100.0,
                    "low": 100.0,
                    "close": 100.0,
                    "volume": 1_000_000.0,
                }
            },
            {},
        ),
        (
            datetime(2026, 8, 11, 13, 30, tzinfo=UTC),
            {
                "SPY": {
                    "open": 100.0,
                    "high": 101.0,
                    "low": 94.0,
                    "close": 94.0,
                    "volume": 1_000_000.0,
                }
            },
            {},
        ),
    ]


def backtest_feed(
    events: list[tuple[datetime, dict[str, dict[str, float]], dict]],
    *,
    feed_spec: FeedSpec | None = None,
) -> DataFeed:
    records = [
        {"timestamp": timestamp, "asset": asset, **values}
        for timestamp, data, _ in events
        for asset, values in data.items()
    ]
    return DataFeed(prices_df=pl.DataFrame(records), feed_spec=feed_spec)


def policy() -> ExecutionPolicy:
    return replace(
        default_live_execution_policy(opening_auction=ExecutionBehavior.CLIENT),
        policy_id="cross-engine-contract-v1",
    )


def _normalized_reconciliation(record) -> dict[str, Any]:
    value = record.to_dict()
    del value["order_id"]
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def run_backtest(
    strategy: Strategy,
    events: list[tuple[datetime, dict[str, dict[str, float]], dict]],
    execution_policy: ExecutionPolicy,
    *,
    feed_spec: FeedSpec | None = None,
) -> EngineEvidence:
    engine = Engine(
        backtest_feed(events, feed_spec=feed_spec),
        strategy,
        replace(BacktestConfig(), retain_lifecycle_history=True),
        execution_policy=execution_policy,
    )
    result = engine.run()
    reconciliations = engine.broker.get_intent_reconciliations()
    return EngineEvidence(
        callback_trace=tuple(
            (phase, callback, _utc(timestamp) if timestamp is not None else None)
            for phase, callback, timestamp in backtest_callback_trace(
                engine.lifecycle_dispatcher.invocations
            )
        ),
        strategy_trace=strategy.trace,
        policy=engine.execution_policy.to_dict(),
        targets=tuple(intent.to_dict() for intent in engine.broker.get_target_intents()),
        children=tuple(child.to_dict() for child in engine.broker.get_child_order_intents()),
        reconciliations=tuple(_normalized_reconciliation(record) for record in reconciliations),
        exit_reason=result.fills[-1].exit_reason if len(result.fills) > 1 else None,
    )


async def run_live(
    strategy: Strategy,
    events: list[tuple[datetime, dict[str, dict[str, float]], dict]],
    execution_policy: ExecutionPolicy,
    tmp_path,
    *,
    feed_spec: FeedSpec | None = None,
) -> EngineEvidence:
    raw = DeterministicLiveBroker()
    safe = SafeBroker(
        raw,
        LiveRiskConfig(
            shadow_mode=True,
            max_position_value=200_000.0,
            max_order_value=200_000.0,
            max_position_shares=10_000,
            max_order_shares=10_000,
            dedup_window_seconds=0.0,
            state_file=str(tmp_path / "cross-engine-state.json"),
        ),
    )
    engine = LiveEngine(
        strategy,
        safe,
        DeterministicLiveFeed(events, feed_spec=feed_spec),
        execution_policy=execution_policy,
    )
    await engine.connect()
    await engine.run()
    reconciliations = engine.strategy_runtime.reconciliations
    states = engine.strategy_runtime.position_rule_states
    return EngineEvidence(
        callback_trace=live_callback_trace(engine.lifecycle_dispatcher.invocations),
        strategy_trace=strategy.trace,
        policy=engine.execution_policy.to_dict(),
        targets=tuple(intent.to_dict() for intent in engine.strategy_runtime.targets),
        children=tuple(child.to_dict() for child in engine.strategy_runtime.children),
        reconciliations=tuple(_normalized_reconciliation(record) for record in reconciliations),
        exit_reason=states[-1].exit_reason.value if states else None,
    )


@pytest.mark.asyncio
async def test_same_strategy_matches_lifecycle_target_rule_and_reconciliation_contract(
    tmp_path,
) -> None:
    events = market_events()
    execution_policy = policy()
    backtest_trace = StrategyTrace()
    live_trace = StrategyTrace()

    backtest = run_backtest(
        ContractStrategy(backtest_trace, target_intent()),
        events,
        execution_policy,
    )
    live = await run_live(
        ContractStrategy(live_trace, target_intent()),
        events,
        execution_policy,
        tmp_path,
    )

    assert live.callback_trace == backtest.callback_trace
    assert live.strategy_trace == backtest.strategy_trace
    assert live.policy == backtest.policy
    assert live.targets == backtest.targets
    assert live.children == backtest.children
    assert live.reconciliations == backtest.reconciliations
    assert live.exit_reason == backtest.exit_reason == "stop_loss"


@pytest.mark.asyncio
async def test_overnight_same_session_target_is_rejected_before_persisting_or_submitting(
    tmp_path,
) -> None:
    spec = FeedSpec(
        time_contract_version=2,
        calendar="CME_Equity",
        timezone="UTC",
        data_frequency=DataFrequency.HOURLY,
        timestamp_semantics=TimestampSemantics.EVENT_TIME,
    )
    timestamp = datetime(2025, 7, 6, 23, 30, tzinfo=UTC)
    events = [(timestamp, market_events()[0][1], {})]
    late = target_intent(
        session=date(2025, 7, 7), decision_time=timestamp, position_rule_policy_id=None
    )

    class LateOvernightStrategy(Strategy):
        def on_data(self, timestamp, data, context, broker) -> None:
            broker.register_target_intent(late)

    backtest = Engine(
        backtest_feed(events, feed_spec=spec), LateOvernightStrategy(), execution_policy=policy()
    )
    with pytest.raises(Exception, match="market_event|future effective session"):
        backtest.run()
    assert not backtest.broker.orders
    assert not backtest.broker.get_target_intents()

    raw = DeterministicLiveBroker()
    safe = SafeBroker(
        raw,
        LiveRiskConfig(shadow_mode=True, state_file=str(tmp_path / "late-overnight-state.json")),
    )
    live = LiveEngine(
        LateOvernightStrategy(),
        safe,
        DeterministicLiveFeed(events, feed_spec=spec),
        execution_policy=policy(),
    )
    await live.connect()
    with pytest.raises(Exception, match="future effective session"):
        await live.run()
    assert not live.strategy_runtime.targets
    assert not safe.positions


@pytest.mark.asyncio
async def test_scheduled_target_matches_when_observable_state_is_equivalent(tmp_path) -> None:
    events = market_events()
    execution_policy = policy()
    initial = target_intent(weight=0.0, position_rule_policy_id=None)
    scheduled = target_intent(
        intent_id="scheduled",
        session=date(2026, 8, 11),
        decision_time=datetime(2026, 8, 10, 13, 29, tzinfo=UTC),
        position_rule_policy_id=None,
    )
    backtest_trace = StrategyTrace()
    live_trace = StrategyTrace()

    backtest = run_backtest(
        ContractStrategy(backtest_trace, initial, scheduled),
        events,
        execution_policy,
    )
    live = await run_live(
        ContractStrategy(live_trace, initial, scheduled),
        events,
        execution_policy,
        tmp_path,
    )

    assert live.targets == backtest.targets
    assert live.children == backtest.children
    assert live.strategy_trace.observations == backtest.strategy_trace.observations


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["spy", " spy "])
async def test_lowercase_close_reduces_the_position_created_by_the_strategy(
    tmp_path, symbol
) -> None:
    class CloseStrategy(Strategy):
        def __init__(self):
            self.observations = []

        def on_data(self, timestamp, data, context, broker):
            if not self.observations:
                broker.submit_order(symbol, 1)
                self.observations.append(broker.get_position("SPY").quantity)
            else:
                position = broker.get_position(symbol)
                self.observations.append(None if position is None else position.quantity)
                broker.close_position(symbol)

    safe = SafeBroker(
        DeterministicLiveBroker(),
        LiveRiskConfig(execution_mode="shadow", state_file=str(tmp_path / "state.json")),
    )
    strategy = CloseStrategy()
    engine = LiveEngine(strategy, safe, DeterministicLiveFeed(market_events()))
    await engine.connect()
    await engine.run()

    assert strategy.observations == [1, 1]
    assert safe.positions == {}


@pytest.mark.asyncio
async def test_shadow_order_identities_do_not_collide_within_ticks_or_after_restart(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("ml4t.live.safety.time.time", lambda: 1_700_000_000.0)
    orders = []

    class SubmitStrategy(Strategy):
        def on_data(self, timestamp, data, context, broker):
            orders.append(broker.submit_order("SPY", 1))
            orders.append(broker.submit_order("SPY", 1))
            orders.append(broker.close_position("SPY"))

    for _ in range(2):
        safe = SafeBroker(
            DeterministicLiveBroker(),
            LiveRiskConfig(
                execution_mode="shadow",
                dedup_window_seconds=0,
                state_file=str(tmp_path / "state.json"),
            ),
        )
        engine = LiveEngine(SubmitStrategy(), safe, DeterministicLiveFeed(market_events()[:1]))
        await engine.connect()
        await engine.run()
        assert safe.positions == {}

    assert len(orders) == 6
    assert len({order.order_id for order in orders}) == 6


@pytest.mark.asyncio
async def test_late_same_session_target_is_rejected_without_submission_in_both_engines(
    tmp_path,
) -> None:
    late = target_intent(decision_time=datetime(2026, 8, 10, 13, 29, tzinfo=UTC))

    class LateStrategy(Strategy):
        def on_data(self, timestamp, data, context, broker) -> None:
            broker.register_target_intent(late, position_rules=StopLoss(0.05))

    backtest = Engine(backtest_feed(market_events()[:1]), LateStrategy(), execution_policy=policy())
    with pytest.raises(Exception, match="market_event|future effective session"):
        backtest.run()
    assert backtest.broker.orders == []
    assert backtest.broker.get_target_intents() == ()

    raw = DeterministicLiveBroker()
    safe = SafeBroker(
        raw,
        LiveRiskConfig(shadow_mode=True, state_file=str(tmp_path / "late-state.json")),
    )
    live = LiveEngine(
        LateStrategy(),
        safe,
        DeterministicLiveFeed(market_events()[:1]),
        execution_policy=policy(),
    )
    await live.connect()
    with pytest.raises(Exception, match="future effective session"):
        await live.run()
    assert live.strategy_runtime.targets == ()
    assert safe.positions == {}


def test_unsupported_native_policy_and_historical_strategy_fail_before_live_connection() -> None:
    raw = DeterministicLiveBroker()
    native_policy = replace(policy(), opening_auction=ExecutionBehavior.BROKER_NATIVE)
    with pytest.raises(UnsupportedLiveCapabilityError, match="opening_auction"):
        LiveEngine(
            ContractStrategy(StrategyTrace(), target_intent()),
            raw,
            DeterministicLiveFeed([]),
            execution_policy=native_policy,
        )
    assert raw.connect_calls == 0

    def historical_prepare(self, broker, timestamps, config=None) -> None:
        return None

    def historical_data(self, timestamp, data, context, broker) -> None:
        return None

    historical_strategy_type = type(
        "HistoricalStrategy",
        (Strategy,),
        {"on_prepare": historical_prepare, "on_data": historical_data},
    )

    with pytest.raises(HistoricalStrategyCompatibilityError, match="on_prepare"):
        LiveEngine(historical_strategy_type(), raw, DeterministicLiveFeed([]))
    assert raw.connect_calls == 0


@pytest.mark.asyncio
async def test_contract_comparator_detects_runtime_branch_fault(tmp_path) -> None:
    class FaultInjectedStrategy(ContractStrategy):
        def on_data(self, timestamp, data, context, broker) -> None:
            super().on_data(timestamp, data, context, broker)
            if type(broker).__name__ == "Broker":
                self.trace.observations[-1]["fault"] = "backtest-only"

    events = market_events()
    execution_policy = policy()
    backtest = run_backtest(
        FaultInjectedStrategy(StrategyTrace(), target_intent()),
        events,
        execution_policy,
    )
    live = await run_live(
        FaultInjectedStrategy(StrategyTrace(), target_intent()),
        events,
        execution_policy,
        tmp_path,
    )

    assert live.strategy_trace != backtest.strategy_trace


@pytest.mark.asyncio
async def test_builtin_schedule_strategy_makes_equal_completed_sequence_decisions(tmp_path) -> None:
    schedule_time = datetime(2026, 8, 10, 20, tzinfo=UTC)

    class ScheduledLongShort(LongShortStrategy):
        signal_column = "signal"
        long_count = 1
        short_count = 1
        position_size = 0.1
        rebalance_schedule = RebalanceSchedule.explicit_timestamps([schedule_time])

        def __init__(self) -> None:
            super().__init__()
            self.decisions: list[tuple[datetime, tuple[str, ...], tuple[str, ...]]] = []

        def rank_assets(self, data):
            long_assets, short_assets = super().rank_assets(data)
            self.decisions.append((schedule_time, tuple(long_assets), tuple(short_assets)))
            return long_assets, short_assets

    events = [
        (
            schedule_time,
            {
                "AAA": {
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.0,
                    "volume": 1_000_000.0,
                    "signals": {"signal": 1.0},
                },
                "BBB": {
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.0,
                    "volume": 1_000_000.0,
                    "signals": {"signal": -1.0},
                },
            },
            {},
        )
    ]
    records = [
        {
            "timestamp": timestamp,
            "asset": asset,
            **{name: value for name, value in values.items() if name != "signals"},
        }
        for timestamp, data, _ in events
        for asset, values in data.items()
    ]
    signals = [
        {
            "timestamp": timestamp,
            "asset": asset,
            **values["signals"],
        }
        for timestamp, data, _ in events
        for asset, values in data.items()
    ]
    backtest_strategy = ScheduledLongShort()
    Engine(
        DataFeed(prices_df=pl.DataFrame(records), signals_df=pl.DataFrame(signals)),
        backtest_strategy,
    ).run()

    live_strategy = ScheduledLongShort()
    raw = DeterministicLiveBroker()
    safe = SafeBroker(
        raw,
        LiveRiskConfig(
            shadow_mode=True,
            max_position_value=200_000.0,
            max_order_value=200_000.0,
            max_position_shares=10_000,
            max_order_shares=10_000,
            dedup_window_seconds=0.0,
            state_file=str(tmp_path / "built-in-schedule-state.json"),
        ),
    )
    live = LiveEngine(
        live_strategy,
        safe,
        DeterministicLiveFeed(events),
        strategy_config=BacktestConfig(),
    )
    await live.connect()
    await live.run()

    assert (
        live_strategy.decisions
        == backtest_strategy.decisions
        == [(schedule_time, ("AAA",), ("BBB",))]
    )


@pytest.mark.parametrize(
    ("calendar", "now", "expected", "boundary"),
    [
        (
            "NYSE",
            datetime(2025, 7, 4, 15, tzinfo=UTC),
            "closed",
            datetime(2025, 7, 7, 13, 30, tzinfo=UTC),
        ),
        (
            "NYSE",
            datetime(2025, 7, 3, 18, tzinfo=UTC),
            "closed",
            datetime(2025, 7, 7, 13, 30, tzinfo=UTC),
        ),
        (
            "HKEX",
            datetime(2025, 7, 7, 4, 30, tzinfo=UTC),
            "break",
            datetime(2025, 7, 7, 5, tzinfo=UTC),
        ),
    ],
)
def test_declared_calendar_prevents_false_open_on_holidays_early_closes_and_breaks(
    calendar: str, now: datetime, expected: str, boundary: datetime
) -> None:
    spec = FeedSpec(
        time_contract_version=2,
        calendar=calendar,
        timestamp_semantics=TimestampSemantics.EVENT_TIME,
    )
    feed = DeterministicLiveFeed([], feed_spec=spec)
    engine = LiveEngine(
        ContractStrategy(StrategyTrace(), target_intent()), DeterministicLiveBroker(), feed
    )

    status = engine.runtime_status(now)

    assert status["session_state"] == expected
    assert status["next_session_boundary"] == boundary


@pytest.mark.parametrize("calendar", [None, "CME_Equity", "NYSE"])
def test_serialized_feed_calendar_reaches_causal_strategy_configuration(
    calendar: str | None,
) -> None:
    source = FeedSpec(time_contract_version=2, calendar=calendar, timezone="UTC")
    payload = json.loads(json.dumps(source.to_dict()))
    projected = FeedSpec.from_mapping(payload)
    engine = LiveEngine(
        ContractStrategy(StrategyTrace(), target_intent()),
        DeterministicLiveBroker(),
        DeterministicLiveFeed([], feed_spec=projected),
    )

    assert engine.strategy_config.resolved_calendar == calendar
    assert engine.strategy_config.resolved_feed_spec.time_contract_version == 2
    assert projected.to_dict() == payload


def test_direct_calendar_none_overrides_declared_feed_calendar_without_mutation() -> None:
    spec = FeedSpec(time_contract_version=2, calendar="CME_Equity")
    engine = LiveEngine(
        ContractStrategy(StrategyTrace(), target_intent()),
        DeterministicLiveBroker(),
        DeterministicLiveFeed([], feed_spec=spec),
        calendar=None,
    )

    assert engine.strategy_config.resolved_calendar is None
    assert (
        engine.runtime_status(datetime(2025, 7, 4, 15, tzinfo=UTC))["session_state"] == "continuous"
    )
    assert spec.calendar == "CME_Equity"


def test_unknown_calendar_rejects_before_broker_connection_or_feed_start() -> None:
    raw = DeterministicLiveBroker()
    feed = DeterministicLiveFeed(
        [], feed_spec=FeedSpec(time_contract_version=2, calendar="not-a-market-calendar")
    )

    with pytest.raises(ValueError, match="calendar"):
        LiveEngine(ContractStrategy(StrategyTrace(), target_intent()), raw, feed)

    assert raw.connect_calls == 0
    assert not raw.pending_orders
    assert not feed._running


class OvernightCalendarStrategy(Strategy):
    def __init__(self, trace: StrategyTrace, intent: CanonicalTargetIntent) -> None:
        self.trace = trace
        self.intent = intent

    def on_start(self, broker) -> None:
        self.trace.callbacks.append("on_start")

    def on_prepare(self, broker, config=None) -> None:
        self.trace.callbacks.append("on_prepare")
        self.trace.observations.append({"resolved_calendar": config.resolved_calendar})
        broker.register_target_intent(self.intent)

    def on_data(self, timestamp, data, context, broker) -> None:
        self.trace.callbacks.append("on_data")
        position = broker.get_position("ES")
        self.trace.observations.append(
            {
                "timestamp": _utc(timestamp).isoformat(),
                "quantity": position.quantity if position else 0,
            }
        )

    def on_end(self, broker) -> None:
        self.trace.callbacks.append("on_end")


@pytest.mark.asyncio
async def test_cme_overnight_target_has_the_same_session_and_observable_position(tmp_path) -> None:
    spec = FeedSpec(
        time_contract_version=2,
        calendar="CME_Equity",
        timezone="UTC",
        data_frequency=DataFrequency.HOURLY,
        timestamp_semantics=TimestampSemantics.EVENT_TIME,
    )
    declared = FeedSpec.from_mapping(json.loads(json.dumps(spec.to_dict())))
    prices = {
        "ES": {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1_000_000.0}
    }
    events = [
        (datetime(2025, 7, 6, 23, 30, tzinfo=UTC), prices, {}),
        (datetime(2025, 7, 7, 0, 30, tzinfo=UTC), prices, {}),
    ]
    initial = replace(
        target_intent(
            session=date(2025, 7, 7),
            decision_time=datetime(2025, 7, 6, 20, tzinfo=UTC),
            position_rule_policy_id=None,
        ),
        targets=(AssetTarget("ES", TargetMeasure.WEIGHT, 0.5),),
    )
    backtest = run_backtest(
        OvernightCalendarStrategy(StrategyTrace(), initial), events, policy(), feed_spec=declared
    )
    live = await run_live(
        OvernightCalendarStrategy(StrategyTrace(), initial),
        events,
        policy(),
        tmp_path,
        feed_spec=declared,
    )

    assert backtest.children
    assert live.children == backtest.children
    assert live.targets == backtest.targets
    assert live.callback_trace == backtest.callback_trace
    assert live.strategy_trace == backtest.strategy_trace


@pytest.mark.asyncio
@pytest.mark.parametrize("calendar", ["HKEX", None, "24/7"])
async def test_feed_freshness_excludes_only_declared_calendar_closures(
    monkeypatch, calendar: str | None
) -> None:
    import ml4t.live.engine as engine_module

    loop = asyncio.get_running_loop()
    observed = asyncio.Event()
    clock = [datetime(2025, 7, 7, 4, tzinfo=UTC)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz)

    class WaitingFeed:
        feed_spec = FeedSpec(
            time_contract_version=2,
            calendar=calendar,
            timestamp_semantics=TimestampSemantics.BAR_CLOSE,
            bar_label=BarLabel.RIGHT,
        )

        def __init__(self):
            self.queue = asyncio.Queue()

        async def start(self):
            self.queue.put_nowait((clock[0], {"0005.HK": {"close": 100.0}}, {}))

        def stop(self):
            self.queue.put_nowait(None)

        def __aiter__(self):
            return self

        async def __anext__(self):
            item = await self.queue.get()
            if item is None:
                raise StopAsyncIteration
            return item

    class Observer(Strategy):
        def on_data(self, timestamp, data, context, broker):
            loop.call_soon_threadsafe(observed.set)

    monkeypatch.setattr(engine_module, "datetime", Clock)
    engine = LiveEngine(
        Observer(),
        DeterministicLiveBroker(),
        WaitingFeed(),
        feed_silence_seconds=5,
        watchdog_poll_seconds=60,
    )
    await engine.connect()
    task = asyncio.create_task(engine.run())
    try:
        await asyncio.wait_for(observed.wait(), timeout=5)
        clock[0] = datetime(2025, 7, 7, 5, 0, 2, tzinfo=UTC)
        status = engine.runtime_status()
        assert status["health"] == ("ok" if calendar == "HKEX" else "feed_silent")
        assert status["last_bar_age_seconds"] == 3602
        assert status["last_bar_trading_age_seconds"] == (2 if calendar == "HKEX" else 3602)
        clock[0] = datetime(2025, 7, 7, 5, 0, 6, tzinfo=UTC)
        assert engine.runtime_status()["health"] == "feed_silent"
    finally:
        await engine.stop()
        await task


@pytest.mark.parametrize("provider", ["okx", "alpaca"])
def test_actual_crypto_feed_declares_weekend_calendar_to_engine(provider: str) -> None:
    from ml4t.live.feeds import OKXFundingFeed

    feed = OKXFundingFeed(["BTC-USDT-SWAP"]) if provider == "okx" else _offline_alpaca_crypto_feed()
    engine = LiveEngine(
        ContractStrategy(StrategyTrace(), target_intent()), DeterministicLiveBroker(), feed
    )
    status = engine.runtime_status(datetime(2025, 7, 6, 15, tzinfo=UTC))
    assert status["session_state"] == "continuous"
    assert status["calendar"] == "24/7"


def _offline_alpaca_crypto_feed():
    from ml4t.live.feeds import AlpacaDataFeed
    from ml4t.live.feeds.experimental import ExperimentalFeedWarning

    with pytest.warns(ExperimentalFeedWarning):
        return AlpacaDataFeed("offline", "offline", ["BTC/USD"], experimental=True)


@pytest.mark.parametrize("calendar", [None, "CME_Equity"])
def test_aggregated_feed_preserves_the_source_calendar_in_engine_health(calendar):
    from ml4t.live.feeds import BarAggregator

    source = DeterministicLiveFeed(
        [], feed_spec=FeedSpec(time_contract_version=2, calendar=calendar)
    )
    feed = BarAggregator(source)
    engine = LiveEngine(
        ContractStrategy(StrategyTrace(), target_intent()), DeterministicLiveBroker(), feed
    )
    status = engine.runtime_status(datetime(2025, 7, 6, 23, 30, tzinfo=UTC))
    assert status["session_state"] == ("continuous" if calendar is None else "open")
    assert status["calendar"] == calendar


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("calendar", "semantics", "expected_indices"),
    [
        ("NYSE", TimestampSemantics.EVENT_TIME, [1]),
        ("NYSE", TimestampSemantics.BAR_CLOSE, [2]),
        (None, TimestampSemantics.EVENT_TIME, [0, 1, 2, 3]),
    ],
)
async def test_session_enforcement_matches_actual_backtest_at_early_close(
    calendar, semantics, expected_indices
):
    class Observer(Strategy):
        def __init__(self):
            self.timestamps = []

        def on_data(self, timestamp, data, context, broker):
            self.timestamps.append(_utc(timestamp))

    timestamps = [
        datetime(2025, 7, 3, hour, minute, tzinfo=UTC)
        for hour, minute in [(13, 0), (13, 30), (17, 0), (17, 1)]
    ]
    data = {"SPY": {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1000.0}}
    events = [(timestamp, data, {}) for timestamp in timestamps]
    spec = FeedSpec(
        time_contract_version=2,
        calendar=calendar,
        data_frequency=DataFrequency.MINUTE_1,
        timestamp_semantics=semantics,
    )
    backtest = Observer()
    Engine(
        backtest_feed(events, feed_spec=spec), backtest, BacktestConfig(enforce_sessions=True)
    ).run()
    expected = [timestamps[index] for index in expected_indices]
    assert backtest.timestamps == expected
    live = Observer()
    engine = LiveEngine(
        live,
        DeterministicLiveBroker(),
        DeterministicLiveFeed(events, feed_spec=spec),
        strategy_config=BacktestConfig(enforce_sessions=True),
    )
    await engine.connect()
    await engine.run()
    assert live.timestamps == backtest.timestamps
