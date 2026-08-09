from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any

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
    ResidualPolicy,
    RoundingPolicy,
    TargetMeasure,
)

from ml4t.live import (
    CanonicalOrderRequest,
    LiveEngine,
    LiveRiskConfig,
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
    class RecoveryTargetStrategy(InitialTargetStrategy):
        def __init__(self, intent, rules) -> None:
            super().__init__(intent, rules)
            self.data_calls = 0

        def on_data(self, timestamp, data, context, broker) -> None:
            self.data_calls += 1

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
        feed_silence_seconds=0.01,
        watchdog_poll_seconds=0.001,
        auto_recover=True,
        recovery_cooldown_seconds=0,
        max_recovery_attempts=1,
    )
    await engine.connect()

    async def stop_after_recovered_bar() -> None:
        while strategy.data_calls < 2:
            await asyncio.sleep(0.001)
        await engine.stop()

    await asyncio.wait_for(
        asyncio.gather(engine.run(), stop_after_recovered_bar()),
        timeout=1,
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
