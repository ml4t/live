from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any, cast

import pytest
from ml4t.backtest import StopLoss, Strategy
from ml4t.backtest.risk.types import PositionAction
from ml4t.backtest.types import Order, OrderSide, OrderStatus, OrderType, Position
from ml4t.specs import (
    AssetTarget,
    CanonicalTargetIntent,
    ExecutionBehavior,
    IntentReason,
    LifecyclePhase,
    LifecycleVersion,
    ResidualPolicy,
    RoundingPolicy,
    TargetMeasure,
)

from ml4t.live import (
    CanonicalOrderRequest,
    LiveEngine,
    LiveIntentError,
    LiveRiskConfig,
    LiveStrategyRuntime,
    ReducingRiskExecutionError,
    SafeBroker,
    UnsupportedLiveCapabilityError,
    default_live_execution_policy,
)


class RuntimeBroker:
    def __init__(self) -> None:
        self._connected = False
        self._positions: dict[str, Position] = {}
        self._pending_orders: list[Order] = []
        self.connect_calls = 0
        self.submit_calls = 0

    @property
    def positions(self) -> dict[str, Position]:
        return dict(self._positions)

    @property
    def pending_orders(self) -> list[Order]:
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

    async def get_positions_async(self) -> dict[str, Position]:
        return self.positions

    async def get_pending_orders_async(self, asset: str | None = None) -> list[Order]:
        if asset is None:
            return self.pending_orders
        return [order for order in self._pending_orders if order.asset == asset]

    async def get_position_async(self, asset: str) -> Position | None:
        return self._positions.get(asset)

    def get_position(self, asset: str) -> Position | None:
        return self._positions.get(asset)

    def assert_paper_trading(self) -> None:
        """Identify this deterministic adapter as a paper venue."""

    def assert_live_trading(self) -> None:
        """Allow tests that explicitly exercise the live routing contract."""

    async def get_account_value_async(self) -> float:
        return 100_000.0

    async def get_cash_async(self) -> float:
        return 100_000.0

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
        self.submit_calls += 1
        request = CanonicalOrderRequest.from_input(
            asset, quantity, side, order_type, limit_price, stop_price
        )
        order = Order(
            asset=request.asset,
            quantity=request.quantity,
            side=request.side,
            order_type=request.order_type,
            limit_price=request.limit_price,
            stop_price=request.stop_price,
            order_id=f"runtime-{self.submit_calls}",
            status=OrderStatus.PENDING,
            created_at=datetime.now(UTC),
        )
        self._pending_orders.append(order)
        return order

    async def cancel_order_async(self, order_id: str) -> bool:
        return False

    async def replace_order_async(self, order_id: str, **kwargs: Any) -> Order:
        raise NotImplementedError

    async def close_position_async(self, asset: str) -> Order | None:
        return None


class PersistentRuntimeBroker(RuntimeBroker):
    def __init__(self) -> None:
        super().__init__()
        self.portable_state: dict[str, Any] = {}

    def load_portable_strategy_state(self) -> dict[str, Any]:
        return self.portable_state

    def save_portable_strategy_state(self, state: dict[str, Any]) -> None:
        self.portable_state = state


class FillBroker(RuntimeBroker):
    def __init__(self, *, partial_quantity: float | None = None, fail_reductions: bool = False):
        super().__init__()
        self.partial_quantity = partial_quantity
        self.fail_reductions = fail_reductions
        self.reduction_keys: list[str] = []

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
        if kwargs.get("reducing_risk"):
            self.reduction_keys.append(kwargs["intent_idempotency_key"])
            if self.fail_reductions:
                raise RuntimeError("venue unavailable")
        self.submit_calls += 1
        resolved_side = side or (OrderSide.BUY if quantity > 0 else OrderSide.SELL)
        filled = min(quantity, self.partial_quantity or quantity)
        signed_fill = filled if resolved_side is OrderSide.BUY else -filled
        prior = self._positions.get(asset)
        new_quantity = (prior.quantity if prior is not None else 0.0) + signed_fill
        if new_quantity == 0:
            self._positions.pop(asset, None)
        else:
            self._positions[asset] = Position(
                asset=asset,
                quantity=new_quantity,
                entry_price=100.0,
                entry_time=datetime(2026, 8, 10, 13, 30, tzinfo=UTC),
                current_price=100.0,
            )
        order = Order(
            asset=asset,
            quantity=quantity,
            side=resolved_side,
            order_type=order_type,
            limit_price=limit_price,
            stop_price=stop_price,
            order_id=f"fill-{self.submit_calls}",
            status=OrderStatus.FILLED if filled == quantity else OrderStatus.PENDING,
            created_at=datetime(2026, 8, 10, 13, 30, tzinfo=UTC),
            filled_quantity=filled,
            filled_price=100.0,
            filled_at=datetime(2026, 8, 10, 13, 30, tzinfo=UTC),
            target_intent_id=kwargs.get("target_intent_id"),
            child_intent_id=kwargs.get("child_intent_id"),
            intent_idempotency_key=kwargs.get("intent_idempotency_key"),
        )
        if order.status is OrderStatus.PENDING:
            self._pending_orders.append(order)
        return order


class PartialReductionBroker(FillBroker):
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
        original_partial = self.partial_quantity
        if kwargs.get("reducing_risk"):
            self.partial_quantity = 100
        try:
            return await super().submit_order_async(
                asset,
                quantity,
                side,
                order_type,
                limit_price,
                stop_price,
                **kwargs,
            )
        finally:
            self.partial_quantity = original_partial


class RuntimeFeed:
    def __init__(self, bars: list[tuple[datetime, dict[str, dict[str, float]], dict]]) -> None:
        self.bars = bars
        self.started = False

    async def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.started or not self.bars:
            raise StopAsyncIteration
        await asyncio.sleep(0)
        return self.bars.pop(0)


class RecoveringRuntimeFeed:
    def __init__(
        self,
        batches: list[list[tuple[datetime, dict[str, dict[str, float]], dict]]],
    ) -> None:
        self.batches = batches
        self.start_calls = 0
        self.stop_calls = 0
        self.started = False
        self._queue: asyncio.Queue = asyncio.Queue()
        self._producer: asyncio.Task | None = None

    async def start(self) -> None:
        self.started = True
        self._queue = asyncio.Queue()
        index = min(self.start_calls, len(self.batches) - 1)
        batch = self.batches[index]
        self.start_calls += 1

        async def produce() -> None:
            for item in batch:
                await self._queue.put(item)

        self._producer = asyncio.create_task(produce())

    def stop(self) -> None:
        self.stop_calls += 1
        self.started = False
        if self._producer is not None:
            self._producer.cancel()
        self._queue.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        return item


def target_intent(
    *,
    intent_id: str = "initial-portfolio",
    session: date = date(2026, 8, 10),
    position_rule_policy_id: str | None = None,
) -> CanonicalTargetIntent:
    decision = datetime(2026, 8, 9, 20, tzinfo=UTC)
    return CanonicalTargetIntent(
        intent_id=intent_id,
        decision_time=decision,
        information_cutoff=decision,
        effective_session=session,
        effective_phase=LifecyclePhase.PRE_OPEN,
        targets=(AssetTarget("SPY", TargetMeasure.WEIGHT, 0.5),),
        idempotency_key=f"{intent_id}-key",
        measure=TargetMeasure.WEIGHT,
        cash_buffer=0.0,
        rounding=RoundingPolicy.TOWARD_ZERO,
        residual=ResidualPolicy.KEEP_CASH,
        reason=IntentReason.REBALANCE,
        position_rule_policy_id=position_rule_policy_id,
    )


def bar(day: int, *, close: float = 100.0, low: float = 100.0):
    return (
        datetime(2026, 8, day, 13, 30, tzinfo=UTC),
        {
            "SPY": {
                "open": 100.0,
                "high": max(100.0, close),
                "low": low,
                "close": close,
                "volume": 1_000_000.0,
            }
        },
        {},
    )


def risk_config(tmp_path, **changes: Any) -> LiveRiskConfig:
    config = LiveRiskConfig(
        shadow_mode=True,
        max_position_value=200_000.0,
        max_order_value=200_000.0,
        max_position_shares=10_000,
        max_order_shares=10_000,
        dedup_window_seconds=0.0,
        state_file=str(tmp_path / "runtime-state.json"),
    )
    for name, value in changes.items():
        setattr(config, name, value)
    if "shadow_mode" in changes and "execution_mode" not in changes:
        config.execution_mode = "shadow" if config.shadow_mode else "paper"
    return config


class InitialTargetStrategy(Strategy):
    def __init__(self, intent: CanonicalTargetIntent, rules=None) -> None:
        self.intent = intent
        self.rules = rules

    def on_prepare(self, broker, config=None) -> None:
        broker.register_target_intent(self.intent, position_rules=self.rules)

    def on_data(self, timestamp, data, context, broker) -> None:
        return None


@pytest.mark.asyncio
async def test_initial_target_matches_backtest_child_and_persists_lineage(tmp_path) -> None:
    raw = RuntimeBroker()
    safe = SafeBroker(raw, risk_config(tmp_path))
    policy = default_live_execution_policy(opening_auction=ExecutionBehavior.CLIENT)
    engine = LiveEngine(
        InitialTargetStrategy(target_intent()),
        safe,
        RuntimeFeed([bar(10)]),
        execution_policy=policy,
    )

    await engine.connect()
    await engine.run()

    child = engine.strategy_runtime.children[0]
    assert child.target_intent_id == "initial-portfolio"
    assert child.asset == "SPY"
    assert child.quantity == 500
    assert engine.strategy_runtime.reconciliations[-1].filled_quantity == 500
    assert safe.positions["SPY"].quantity == 500
    persisted = safe.load_portable_strategy_state()
    assert persisted["targets"][0] == target_intent().to_dict()
    assert persisted["children"][0] == child.to_dict()


@pytest.mark.asyncio
async def test_recovery_preserves_target_pending_and_rule_state_without_duplicate_intent(
    tmp_path,
) -> None:
    recovered_bar = asyncio.Event()

    class RecoveryTargetStrategy(InitialTargetStrategy):
        def __init__(self, intent, rules) -> None:
            super().__init__(intent, rules)
            self.data_calls = 0

        def on_data(self, timestamp, data, context, broker) -> None:
            self.data_calls += 1
            if self.data_calls == 2:
                recovered_bar.set()

    raw = FillBroker(partial_quantity=250)
    safe = SafeBroker(raw, risk_config(tmp_path, shadow_mode=False))
    intent = target_intent(position_rule_policy_id="recovery-stop")
    strategy = RecoveryTargetStrategy(intent, StopLoss(0.05))
    feed = RecoveringRuntimeFeed([[bar(10)], [bar(10, close=101.0)]])
    engine = LiveEngine(
        strategy,
        safe,
        feed,
        execution_policy=default_live_execution_policy(opening_auction=ExecutionBehavior.CLIENT),
        feed_silence_seconds=0.5,
        watchdog_poll_seconds=0.01,
        auto_recover=True,
        recovery_cooldown_seconds=0,
        max_recovery_attempts=1,
    )
    await engine.connect()

    async def stop_after_recovered_bar() -> None:
        await recovered_bar.wait()
        await engine.stop()

    await asyncio.wait_for(
        asyncio.gather(engine.run(), stop_after_recovered_bar()),
        timeout=15,
    )

    assert raw.connect_calls == 2
    assert raw.submit_calls == 1
    assert len(raw.pending_orders) == 1
    assert len(engine.strategy_runtime.targets) == 1
    assert len(engine.strategy_runtime.children) == 1
    assert len(engine.strategy_runtime.position_rule_states) == 1
    assert len(engine.strategy_runtime.reconciliations) == 1
    assert engine.stats["callback_counts"]["run_start"] == 1
    assert engine.stats["callback_counts"]["causal_initialization"] == 1
    assert engine.stats["callback_counts"]["run_end"] == 1
    persisted = safe.load_portable_strategy_state()
    assert len(persisted["targets"]) == 1
    assert len(persisted["children"]) == 1
    assert len(persisted["position_rule_states"]) == 1


@pytest.mark.asyncio
async def test_scheduled_target_executes_at_later_open(tmp_path) -> None:
    intent = target_intent(intent_id="scheduled", session=date(2026, 8, 11))

    class ScheduledStrategy(Strategy):
        def on_data(self, timestamp, data, context, broker) -> None:
            if timestamp.date() == date(2026, 8, 10):
                broker.register_target_intent(intent)

    safe = SafeBroker(RuntimeBroker(), risk_config(tmp_path))
    engine = LiveEngine(
        ScheduledStrategy(),
        safe,
        RuntimeFeed([bar(10), bar(11)]),
        execution_policy=default_live_execution_policy(opening_auction=ExecutionBehavior.CLIENT),
    )

    await engine.connect()
    await engine.run()

    assert engine.strategy_runtime.reconciliations[-1].event_time.date() == date(2026, 8, 11)
    assert safe.positions["SPY"].quantity == 500


@pytest.mark.asyncio
async def test_restart_deduplicates_target_and_restores_shadow_position(tmp_path) -> None:
    config = risk_config(tmp_path)
    policy = default_live_execution_policy(opening_auction=ExecutionBehavior.CLIENT)
    first = SafeBroker(RuntimeBroker(), config)
    first_engine = LiveEngine(
        InitialTargetStrategy(target_intent()),
        first,
        RuntimeFeed([bar(10)]),
        execution_policy=policy,
    )
    await first_engine.connect()
    await first_engine.run()
    await first_engine.stop()

    restarted = SafeBroker(RuntimeBroker(), config)
    second_engine = LiveEngine(
        InitialTargetStrategy(target_intent()),
        restarted,
        RuntimeFeed([]),
        execution_policy=policy,
    )
    await second_engine.connect()
    await second_engine.run()

    assert restarted.positions["SPY"].quantity == 500
    assert len(second_engine.strategy_runtime.targets) == 1
    assert len(second_engine.strategy_runtime.children) == 1
    assert len(second_engine.strategy_runtime.reconciliations) == 1


@pytest.mark.asyncio
async def test_partial_fill_and_remaining_intent_survive_restart_without_resubmission(
    tmp_path,
) -> None:
    raw = FillBroker(partial_quantity=100)
    config = risk_config(tmp_path, shadow_mode=False)
    policy = default_live_execution_policy(opening_auction=ExecutionBehavior.CLIENT)
    intent = target_intent(position_rule_policy_id="stop-5")
    first = LiveEngine(
        InitialTargetStrategy(intent, StopLoss(0.05)),
        SafeBroker(raw, config),
        RuntimeFeed([bar(10)]),
        execution_policy=policy,
    )
    await first.connect()
    await first.run()
    await first.stop()
    raw._pending_orders[0].filled_quantity = 250
    raw._positions["SPY"].quantity = 250

    restarted = LiveEngine(
        InitialTargetStrategy(intent, StopLoss(0.05)),
        SafeBroker(raw, config),
        RuntimeFeed([bar(11)]),
        execution_policy=policy,
    )
    await restarted.connect()
    await restarted.run()

    reconciliation = restarted.strategy_runtime.reconciliations[-1]
    assert reconciliation.filled_quantity == 250
    assert reconciliation.remaining_quantity == 250
    assert reconciliation.outcome.value == "partial"
    assert restarted.strategy_runtime.position_rule_states[0].remaining_exit_quantity == 250
    assert raw.submit_calls == 1


def test_unsupported_native_opening_policy_fails_before_connect() -> None:
    raw = RuntimeBroker()
    policy = replace(
        default_live_execution_policy(),
        opening_auction=ExecutionBehavior.BROKER_NATIVE,
    )

    with pytest.raises(UnsupportedLiveCapabilityError, match="opening_auction"):
        LiveEngine(
            InitialTargetStrategy(target_intent()), raw, RuntimeFeed([]), execution_policy=policy
        )

    assert raw.connect_calls == 0


def test_unsupported_native_position_rules_fail_before_connect() -> None:
    raw = RuntimeBroker()
    policy = replace(
        default_live_execution_policy(),
        contingent=ExecutionBehavior.BROKER_NATIVE,
    )

    with pytest.raises(UnsupportedLiveCapabilityError, match="contingent"):
        LiveEngine(
            InitialTargetStrategy(target_intent()),
            raw,
            RuntimeFeed([]),
            execution_policy=policy,
        )

    assert raw.connect_calls == 0


@pytest.mark.asyncio
async def test_post_fill_rule_state_survives_restart_and_exits_under_kill_switch(tmp_path) -> None:
    config = risk_config(tmp_path, allow_reducing_risk_when_killed=True)
    safe = SafeBroker(RuntimeBroker(), config)
    intent = target_intent(position_rule_policy_id="stop-5")

    class KillAfterEntryStrategy(InitialTargetStrategy):
        def on_data(self, timestamp, data, context, broker) -> None:
            if timestamp.date() == date(2026, 8, 10):
                broker.update_position_context("SPY", {"regime": "risk_off"})
                safe.enable_kill_switch("test after entry")

    first_engine = LiveEngine(
        KillAfterEntryStrategy(intent, StopLoss(0.05)),
        safe,
        RuntimeFeed([bar(10)]),
        execution_policy=default_live_execution_policy(opening_auction=ExecutionBehavior.CLIENT),
    )

    await first_engine.connect()
    await first_engine.run()
    await first_engine.stop()
    assert safe.load_portable_strategy_state()["position_rule_states"][0]["context"] == {
        "regime": "risk_off"
    }

    restarted_safe = SafeBroker(RuntimeBroker(), config)
    engine = LiveEngine(
        InitialTargetStrategy(intent, StopLoss(0.05)),
        restarted_safe,
        RuntimeFeed([bar(11, close=94.0, low=94.0)]),
        execution_policy=default_live_execution_policy(opening_auction=ExecutionBehavior.CLIENT),
    )
    await engine.connect()
    await engine.run()

    state = engine.strategy_runtime.position_rule_states[0]
    assert state.remaining_exit_quantity == 0
    assert state.exit_reason.value == "stop_loss"
    assert state.low_water_mark == 94
    assert state.max_adverse_excursion == pytest.approx(-0.06)
    assert restarted_safe.positions == {}
    persisted = restarted_safe.load_portable_strategy_state()
    assert persisted["position_rule_states"][0]["remaining_exit_quantity"] == 0


@pytest.mark.asyncio
async def test_kill_switch_policy_can_block_rule_reduction_explicitly(tmp_path) -> None:
    config = risk_config(tmp_path, allow_reducing_risk_when_killed=False)
    safe = SafeBroker(RuntimeBroker(), config)
    intent = target_intent(position_rule_policy_id="stop-5")

    class KillAfterEntryStrategy(InitialTargetStrategy):
        def on_data(self, timestamp, data, context, broker) -> None:
            if timestamp.date() == date(2026, 8, 10):
                safe.enable_kill_switch("block reductions")

    engine = LiveEngine(
        KillAfterEntryStrategy(intent, StopLoss(0.05)),
        safe,
        RuntimeFeed([bar(10), bar(11, close=94.0, low=94.0)]),
        execution_policy=default_live_execution_policy(opening_auction=ExecutionBehavior.CLIENT),
    )
    await engine.connect()

    with pytest.raises(ReducingRiskExecutionError, match="Kill switch policy"):
        await engine.run()

    assert safe.positions["SPY"].quantity == 500


@pytest.mark.asyncio
@pytest.mark.parametrize("halt_on_failure", [True, False])
async def test_venue_failure_policy_halts_or_retries_with_stable_idempotency(
    tmp_path,
    halt_on_failure: bool,
) -> None:
    raw = FillBroker(fail_reductions=True)
    safe = SafeBroker(
        raw,
        risk_config(
            tmp_path,
            shadow_mode=False,
            halt_on_reducing_risk_failure=halt_on_failure,
        ),
    )
    intent = target_intent(position_rule_policy_id="stop-5")
    engine = LiveEngine(
        InitialTargetStrategy(intent, StopLoss(0.05)),
        safe,
        RuntimeFeed(
            [
                bar(10),
                bar(11, close=94.0, low=94.0),
                bar(12, close=93.0, low=93.0),
            ]
        ),
        execution_policy=default_live_execution_policy(opening_auction=ExecutionBehavior.CLIENT),
    )
    await engine.connect()

    if halt_on_failure:
        with pytest.raises(ReducingRiskExecutionError, match="venue unavailable"):
            await engine.run()
        assert len(raw.reduction_keys) == 1
    else:
        await engine.run()
        assert len(raw.reduction_keys) == 2
        assert len(set(raw.reduction_keys)) == 1
    assert raw.positions["SPY"].quantity == 500


class HalfExitRule:
    def evaluate(self, position) -> PositionAction:
        return PositionAction.exit_partial(0.5, reason="scale_down")


@pytest.mark.asyncio
async def test_partial_rule_exit_persists_exact_position_and_rule_remainder(tmp_path) -> None:
    config = risk_config(tmp_path)
    intent = target_intent(position_rule_policy_id="half-exit")
    first_safe = SafeBroker(RuntimeBroker(), config)
    first = LiveEngine(
        InitialTargetStrategy(intent, HalfExitRule()),
        first_safe,
        RuntimeFeed([bar(10), bar(11)]),
        execution_policy=default_live_execution_policy(opening_auction=ExecutionBehavior.CLIENT),
    )
    await first.connect()
    await first.run()
    await first.stop()

    restarted_safe = SafeBroker(RuntimeBroker(), config)
    restarted = LiveEngine(
        InitialTargetStrategy(intent, HalfExitRule()),
        restarted_safe,
        RuntimeFeed([]),
        execution_policy=default_live_execution_policy(opening_auction=ExecutionBehavior.CLIENT),
    )
    await restarted.connect()
    await restarted.run()

    state = restarted.strategy_runtime.position_rule_states[0]
    assert restarted_safe.positions["SPY"].quantity == 250
    assert state.remaining_exit_quantity == 250
    assert state.action.value == "exit_partial"


@pytest.mark.asyncio
async def test_pending_partial_rule_exit_reconciles_cumulative_fills_without_duplicate(
    tmp_path,
) -> None:
    raw = PartialReductionBroker()
    safe = SafeBroker(raw, risk_config(tmp_path, shadow_mode=False))
    intent = target_intent(position_rule_policy_id="half-exit")

    class AdvancePartialFillStrategy(InitialTargetStrategy):
        def on_data(self, timestamp, data, context, broker) -> None:
            if timestamp.date() != date(2026, 8, 11):
                return
            exit_order = raw.pending_orders[0]
            exit_order.filled_quantity = 200
            raw._positions["SPY"].quantity = 300

    engine = LiveEngine(
        AdvancePartialFillStrategy(intent, HalfExitRule()),
        safe,
        RuntimeFeed([bar(10), bar(11), bar(12)]),
        execution_policy=default_live_execution_policy(opening_auction=ExecutionBehavior.CLIENT),
    )
    await engine.connect()
    await engine.run()

    state = engine.strategy_runtime.position_rule_states[0]
    assert state.remaining_exit_quantity == 300
    assert raw.positions["SPY"].quantity == 300
    assert raw.submit_calls == 2
    persisted = safe.load_portable_strategy_state()
    assert next(iter(persisted["rule_exit_filled"].values())) == 200


@pytest.mark.asyncio
async def test_strategy_facade_set_clear_and_context_apply_to_direct_fills(tmp_path) -> None:
    raw = FillBroker()
    safe = SafeBroker(raw, risk_config(tmp_path, shadow_mode=False))

    class DirectOrderStrategy(Strategy):
        def on_prepare(self, broker, config=None) -> None:
            broker.set_position_rules(StopLoss(0.05))
            broker.set_position_rules(StopLoss(0.20), asset="SPY")
            broker.clear_position_rules("SPY")
            broker.set_position_rules(StopLoss(0.05), asset="SPY")

        def on_data(self, timestamp, data, context, broker) -> None:
            if timestamp.date() == date(2026, 8, 10):
                broker.submit_order("SPY", 10, OrderSide.BUY)
                broker.update_position_context("SPY", {"source": "direct"})

    engine = LiveEngine(
        DirectOrderStrategy(),
        safe,
        RuntimeFeed([bar(10), bar(11, close=94.0, low=94.0)]),
    )
    await engine.connect()
    await engine.run()

    state = engine.strategy_runtime.position_rule_states[0]
    persisted = safe.load_portable_strategy_state()["position_rule_states"][0]
    assert state.exit_reason.value == "stop_loss"
    assert raw.positions == {}
    assert persisted["context"] == {"source": "direct"}


def direct_runtime(
    broker: RuntimeBroker | None = None,
    *,
    opening_auction: ExecutionBehavior = ExecutionBehavior.CLIENT,
    contingent: ExecutionBehavior = ExecutionBehavior.CLIENT,
) -> LiveStrategyRuntime:
    policy = replace(
        default_live_execution_policy(opening_auction=opening_auction),
        contingent=contingent,
    )
    return LiveStrategyRuntime(broker or PersistentRuntimeBroker(), policy, LifecycleVersion.V1)


def test_target_registration_requires_persistence_and_causal_phase() -> None:
    without_persistence = direct_runtime(RuntimeBroker())
    with pytest.raises(UnsupportedLiveCapabilityError, match="persistent strategy state"):
        without_persistence.register_target_intent(target_intent())

    runtime = direct_runtime()
    with pytest.raises(LiveIntentError, match="causal initialization or a market event"):
        runtime.register_target_intent(target_intent())

    runtime = LiveStrategyRuntime(
        PersistentRuntimeBroker(),
        default_live_execution_policy(opening_auction=ExecutionBehavior.CLIENT),
        cast(LifecycleVersion, "2"),
    )
    runtime.active_phase = LifecyclePhase.CAUSAL_INITIALIZATION
    with pytest.raises(LiveIntentError, match="lifecycle version"):
        runtime.register_target_intent(target_intent())


def test_target_registration_rejects_phase_policy_and_rule_mismatches() -> None:
    runtime = direct_runtime()
    runtime.active_phase = LifecyclePhase.CAUSAL_INITIALIZATION
    with pytest.raises(LiveIntentError, match="effective_phase must be pre_open"):
        runtime.register_target_intent(
            replace(target_intent(), effective_phase=LifecyclePhase.MARKET_EVENT)
        )

    disabled = direct_runtime(opening_auction=ExecutionBehavior.DISABLED)
    disabled.active_phase = LifecyclePhase.CAUSAL_INITIALIZATION
    with pytest.raises(UnsupportedLiveCapabilityError, match="disables opening_auction"):
        disabled.register_target_intent(target_intent())

    rules = StopLoss(0.05)
    with pytest.raises(LiveIntentError, match="require position_rule_policy_id"):
        runtime.register_target_intent(target_intent(), position_rules=rules)

    missing_policy = target_intent(position_rule_policy_id="missing-policy")
    with pytest.raises(UnsupportedLiveCapabilityError, match="no client implementation"):
        runtime.register_target_intent(missing_policy)


def test_target_registration_rejects_late_duplicate_and_overlapping_intents() -> None:
    runtime = direct_runtime()
    runtime.active_phase = LifecyclePhase.MARKET_EVENT
    runtime.current_event_time = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    with pytest.raises(LiveIntentError, match="future effective session"):
        runtime.register_target_intent(target_intent())

    runtime.active_phase = LifecyclePhase.CAUSAL_INITIALIZATION
    runtime.current_event_time = None
    first = runtime.register_target_intent(target_intent())
    with pytest.raises(LiveIntentError, match="different target"):
        runtime.register_target_intent(
            replace(
                first,
                intent_id="different-id",
                targets=(AssetTarget("SPY", TargetMeasure.WEIGHT, 0.4),),
            )
        )
    with pytest.raises(LiveIntentError, match="duplicate target intent_id"):
        runtime.register_target_intent(replace(first, idempotency_key="different-key"))
    with pytest.raises(LiveIntentError, match="target overlaps"):
        runtime.register_target_intent(
            replace(first, intent_id="overlap", idempotency_key="overlap-key")
        )


def test_position_rule_registration_rejects_empty_conflicting_and_native_policies() -> None:
    runtime = direct_runtime()
    rules = StopLoss(0.05)
    with pytest.raises(ValueError, match="non-empty"):
        runtime.register_position_rule_policy("", rules)
    runtime.register_position_rule_policy("stop", rules)
    with pytest.raises(LiveIntentError, match="already registered"):
        runtime.register_position_rule_policy("stop", StopLoss(0.10))

    native = direct_runtime()
    native.policy = replace(native.policy, contingent=ExecutionBehavior.BROKER_NATIVE)
    with pytest.raises(UnsupportedLiveCapabilityError, match="broker-native position rules"):
        native.set_position_rules(rules)


def test_target_registration_rejects_conflicting_rule_implementation() -> None:
    runtime = direct_runtime()
    runtime.active_phase = LifecyclePhase.CAUSAL_INITIALIZATION
    runtime.register_target_intent(
        target_intent(position_rule_policy_id="stop"),
        position_rules=StopLoss(0.05),
    )

    with pytest.raises(LiveIntentError, match="already registered"):
        runtime.register_target_intent(
            target_intent(intent_id="second", position_rule_policy_id="stop"),
            position_rules=StopLoss(0.10),
        )


@pytest.mark.asyncio
async def test_runtime_noop_observation_context_and_repeated_target_are_stable() -> None:
    broker = PersistentRuntimeBroker()
    broker._positions["SPY"] = Position(
        "SPY",
        1,
        100,
        datetime(2026, 8, 9, tzinfo=UTC),
    )
    runtime = direct_runtime(broker)
    runtime.observe_strategy_order(
        Order(
            asset="SPY",
            quantity=1,
            side=OrderSide.BUY,
            order_id="pending",
            status=OrderStatus.PENDING,
        )
    )
    runtime.update_position_context("SPY", {"regime": "risk_off"})
    assert broker.positions["SPY"].context == {"regime": "risk_off"}

    runtime.active_phase = LifecyclePhase.CAUSAL_INITIALIZATION
    runtime.register_target_intent(target_intent())
    event_time = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    data = {"SPY": {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0}}
    await runtime.process_market_event(event_time, data, {})
    submit_calls = broker.submit_calls
    await runtime.process_market_event(event_time, data, {})

    assert broker.submit_calls == submit_calls


@pytest.mark.asyncio
async def test_future_and_already_satisfied_targets_submit_no_child_orders() -> None:
    future_broker = PersistentRuntimeBroker()
    future = direct_runtime(future_broker)
    future.active_phase = LifecyclePhase.CAUSAL_INITIALIZATION
    future.register_target_intent(target_intent(session=date(2026, 8, 11)))
    event_time = datetime(2026, 8, 10, 13, 30, tzinfo=UTC)
    data = {"SPY": {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0}}
    await future.process_market_event(event_time, data, {})
    assert future_broker.submit_calls == 0

    satisfied_broker = PersistentRuntimeBroker()
    satisfied_broker._positions["SPY"] = Position(
        "SPY",
        500,
        100,
        datetime(2026, 8, 9, tzinfo=UTC),
    )
    satisfied = direct_runtime(satisfied_broker)
    satisfied.active_phase = LifecyclePhase.CAUSAL_INITIALIZATION
    satisfied.register_target_intent(target_intent())
    await satisfied.process_market_event(event_time, data, {})

    assert satisfied.children == ()
    assert satisfied_broker.submit_calls == 0


@pytest.mark.asyncio
async def test_target_processing_rejects_late_missing_data_and_rounding_residuals() -> None:
    late = direct_runtime()
    late.active_phase = LifecyclePhase.CAUSAL_INITIALIZATION
    late.register_target_intent(target_intent())
    with pytest.raises(LiveIntentError, match="missed its effective session"):
        await late.process_market_event(
            datetime(2026, 8, 11, 13, 30, tzinfo=UTC),
            {"SPY": {"open": 100.0}},
            {},
        )

    missing = direct_runtime()
    missing.active_phase = LifecyclePhase.CAUSAL_INITIALIZATION
    missing.register_target_intent(target_intent())
    with pytest.raises(LiveIntentError, match="no data for SPY"):
        await missing.process_market_event(datetime(2026, 8, 10, 13, 30, tzinfo=UTC), {}, {})

    residual = direct_runtime()
    residual.active_phase = LifecyclePhase.CAUSAL_INITIALIZATION
    residual.register_target_intent(
        replace(
            target_intent(),
            targets=(AssetTarget("SPY", TargetMeasure.WEIGHT, 0.000001),),
            residual=ResidualPolicy.REJECT,
        )
    )
    with pytest.raises(LiveIntentError, match="rounding residual"):
        await residual.process_market_event(
            datetime(2026, 8, 10, 13, 30, tzinfo=UTC),
            {"SPY": {"open": 100.0}},
            {},
        )


@pytest.mark.parametrize("value", [True, "100", float("nan"), 0])
def test_runtime_price_validation_rejects_nonpositive_nonfinite_and_nonnumeric(value) -> None:
    with pytest.raises(LiveIntentError, match="open price"):
        LiveStrategyRuntime._price({"open": value}, "open")


def test_runtime_rounding_exit_reason_and_naive_time_normalization() -> None:
    assert LiveStrategyRuntime._round(1.9, RoundingPolicy.NONE) == 1.9
    assert LiveStrategyRuntime._round(-1.9, RoundingPolicy.TOWARD_ZERO) == -1
    assert LiveStrategyRuntime._round(-1.5, RoundingPolicy.NEAREST) == -2
    assert LiveStrategyRuntime._exit_reason("take_profit:rule") == "take_profit"
    assert LiveStrategyRuntime._exit_reason("trailing_stop:rule") == "trailing_stop"
    assert LiveStrategyRuntime._exit_reason("time_exit:rule") == "time_exit"
    assert LiveStrategyRuntime._exit_reason("signal") == "signal"
    assert LiveStrategyRuntime._as_utc(datetime(2026, 8, 9)).tzinfo is UTC
