"""Portable target-intent and position-rule runtime for live execution."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ml4t.backtest import IntentOutcome, IntentReconciliation
from ml4t.backtest.risk.types import ActionType, PositionState
from ml4t.backtest.types import Order, OrderSide, OrderStatus, OrderType
from ml4t.specs import (
    BarPathPolicy,
    CanonicalChildOrderIntent,
    CanonicalTargetIntent,
    EvaluationMode,
    ExecutionBehavior,
    ExecutionCapability,
    ExecutionPolicy,
    ExitReason,
    FillEligibility,
    LifecyclePhase,
    LifecycleVersion,
    OrderParameters,
    PositionActionType,
    PositionRuleState,
    ResidualPolicy,
    RoundingPolicy,
    RuleActivation,
    SessionPolicy,
    TargetMeasure,
    TimeInForce,
)
from ml4t.specs import OrderSide as SpecOrderSide
from ml4t.specs import OrderType as SpecOrderType

from .persistence import redact_sensitive

if TYPE_CHECKING:
    from ml4t.backtest.risk.position import PositionRule

    from .protocols import AsyncBrokerProtocol


class LiveStrategyRuntimeError(RuntimeError):
    """Base class for live portable-strategy failures."""


class UnsupportedLiveCapabilityError(LiveStrategyRuntimeError):
    """Raised before connection or submission for an unsupported declared capability."""


class LiveIntentError(LiveStrategyRuntimeError):
    """Raised when a target is late, conflicting, or cannot be lowered safely."""


class ReducingRiskExecutionError(LiveStrategyRuntimeError):
    """Raised when a client-evaluated protective action cannot be submitted safely."""


def default_live_execution_policy(
    *,
    opening_auction: ExecutionBehavior = ExecutionBehavior.DISABLED,
) -> ExecutionPolicy:
    """Return explicit conservative live assumptions without native substitution."""
    return ExecutionPolicy(
        policy_id="ml4t-live-client-v1",
        market_fill_phase=LifecyclePhase.MARKET_EVENT,
        opening_auction=opening_auction,
        close_auction=ExecutionBehavior.BROKER_NATIVE,
        limit=ExecutionBehavior.BROKER_NATIVE,
        stop=ExecutionBehavior.BROKER_NATIVE,
        stop_limit=ExecutionBehavior.BROKER_NATIVE,
        trailing=ExecutionBehavior.CLIENT,
        contingent=ExecutionBehavior.CLIENT,
        fee_bps=0.0,
        slippage_bps=0.0,
        spread_bps=0.0,
        impact_bps=0.0,
        latency_ms=0.0,
        liquidity_fraction=1.0,
        allow_partial_fills=True,
        bar_path=BarPathPolicy.REJECT_AMBIGUOUS,
    )


class LiveStrategyRuntime:
    """Persist, lower, reconcile, and evaluate portable strategy intent."""

    def __init__(
        self,
        broker: AsyncBrokerProtocol,
        policy: ExecutionPolicy,
        lifecycle_version: LifecycleVersion,
    ) -> None:
        self.broker = broker
        self.policy = policy
        self.lifecycle_version = lifecycle_version
        self.active_phase: LifecyclePhase | None = None
        self.current_event_time: datetime | None = None
        self._targets: dict[str, CanonicalTargetIntent] = {}
        self._idempotency: dict[str, str] = {}
        self._children: dict[str, CanonicalChildOrderIntent] = {}
        self._orders: dict[str, Order] = {}
        self._order_by_child: dict[str, str] = {}
        self._processed_targets: set[str] = set()
        self._reconciliations: list[IntentReconciliation] = []
        self._latest_reconciliation: dict[str, IntentReconciliation] = {}
        self._rules_by_policy: dict[str, PositionRule] = {}
        self._global_rules: PositionRule | None = None
        self._rules_by_asset: dict[str, PositionRule | None] = {}
        self._rule_states: dict[str, PositionRuleState] = {}
        self._rule_context: dict[str, dict[str, Any]] = {}
        self._duration_events: dict[str, int] = {}
        self._exit_idempotency: set[str] = set()
        self._rule_exit_orders: dict[str, dict[str, Any]] = {}
        self._rule_exit_filled: dict[str, float] = {}
        self._target_rule_filled: dict[str, float] = {}
        self._validate_preconnect_capabilities()
        self._restore()

    @property
    def targets(self) -> tuple[CanonicalTargetIntent, ...]:
        return tuple(self._targets.values())

    @property
    def children(self) -> tuple[CanonicalChildOrderIntent, ...]:
        return tuple(self._children.values())

    @property
    def reconciliations(self) -> tuple[IntentReconciliation, ...]:
        return tuple(self._reconciliations)

    @property
    def position_rule_states(self) -> tuple[PositionRuleState, ...]:
        return tuple(self._rule_states.values())

    def register_target_intent(
        self,
        intent: CanonicalTargetIntent,
        *,
        position_rules: PositionRule | None = None,
    ) -> CanonicalTargetIntent:
        """Register one causal, restart-safe target from a strategy callback."""
        if not hasattr(self.broker, "save_portable_strategy_state"):
            raise UnsupportedLiveCapabilityError(
                "live target intents require SafeBroker persistent strategy state"
            )
        if intent.lifecycle_version is not self.lifecycle_version:
            raise LiveIntentError("target lifecycle version does not match LiveEngine")
        if intent.effective_phase is not LifecyclePhase.PRE_OPEN:
            raise LiveIntentError("opening target effective_phase must be pre_open")
        if self.policy.opening_auction is ExecutionBehavior.DISABLED:
            raise UnsupportedLiveCapabilityError(
                f"execution policy {self.policy.policy_id!r} disables opening_auction"
            )
        if self.active_phase not in {
            LifecyclePhase.CAUSAL_INITIALIZATION,
            LifecyclePhase.MARKET_EVENT,
        }:
            raise LiveIntentError("target intents require causal initialization or a market event")
        if (
            self.active_phase is LifecyclePhase.MARKET_EVENT
            and self.current_event_time is not None
            and intent.effective_session <= self.current_event_time.date()
        ):
            raise LiveIntentError("a market-event target must use a future effective session")
        policy_id = intent.position_rule_policy_id
        if position_rules is not None and policy_id is None:
            raise LiveIntentError("position_rules require position_rule_policy_id")
        if position_rules is not None:
            assert policy_id is not None
            existing_rules = self._rules_by_policy.get(policy_id)
            if existing_rules is not None and existing_rules != position_rules:
                raise LiveIntentError(f"position rule policy {policy_id!r} is already registered")
        elif policy_id is not None and policy_id not in self._rules_by_policy:
            raise UnsupportedLiveCapabilityError(
                f"position rule policy {policy_id!r} has no client implementation"
            )
        existing_id = self._idempotency.get(intent.idempotency_key)
        if existing_id is not None:
            existing = self._targets[existing_id]
            if existing != intent:
                raise LiveIntentError("idempotency key identifies a different target")
            if position_rules is not None:
                assert policy_id is not None
                self.register_position_rule_policy(policy_id, position_rules)
                for state in self._rule_states.values():
                    if state.policy_id == policy_id:
                        self._rules_by_asset[state.asset] = position_rules
            return existing
        if intent.intent_id in self._targets:
            raise LiveIntentError(f"duplicate target intent_id {intent.intent_id!r}")
        assets = {target.asset for target in intent.targets}
        for existing in self._targets.values():
            overlap = assets.intersection(target.asset for target in existing.targets)
            if existing.effective_session == intent.effective_session and overlap:
                raise LiveIntentError(
                    f"target overlaps {existing.intent_id!r} for {', '.join(sorted(overlap))}"
                )
        if position_rules is not None:
            assert policy_id is not None
            self.register_position_rule_policy(policy_id, position_rules)
        self._targets[intent.intent_id] = intent
        self._idempotency[intent.idempotency_key] = intent.intent_id
        self._persist()
        return intent

    def register_position_rule_policy(self, policy_id: str, rules: PositionRule) -> None:
        """Bind a portable policy identity to a client-evaluated rule."""
        if not policy_id:
            raise ValueError("policy_id must be non-empty")
        existing = self._rules_by_policy.get(policy_id)
        if existing is not None and existing != rules:
            raise LiveIntentError(f"position rule policy {policy_id!r} is already registered")
        self._rules_by_policy[policy_id] = rules

    def set_position_rules(self, rules: PositionRule | None, asset: str | None = None) -> None:
        """Set client-evaluated rules globally or for one asset."""
        if self.policy.contingent is ExecutionBehavior.BROKER_NATIVE:
            raise UnsupportedLiveCapabilityError(
                "broker-native position rules require a venue-native policy implementation"
            )
        if asset is None:
            self._global_rules = rules
        else:
            self._rules_by_asset[asset] = rules

    def clear_position_rules(self, asset: str | None = None) -> None:
        self.set_position_rules(None, asset=asset)

    def update_position_context(self, asset: str, context: dict[str, Any]) -> None:
        self._rule_context.setdefault(asset, {}).update(context)
        position = self._position(asset)
        if position is not None:
            position.context.update(context)
        self._persist()

    async def process_market_event(
        self,
        timestamp: datetime,
        data: dict[str, dict[str, Any]],
        context: dict[str, Any],
    ) -> None:
        """Process eligible targets, fills, and client rules before the strategy event."""
        timestamp = self._as_utc(timestamp)
        await self._reconcile(timestamp)
        reconciled_rule_assets = await self._reconcile_rule_exits()
        await self._process_targets(timestamp, data)
        await self._activate_untracked_positions(timestamp)
        await self._evaluate_rules(
            timestamp,
            data,
            context,
            skip_assets=reconciled_rule_assets,
        )
        self._persist()

    def observe_strategy_order(self, order: Order) -> None:
        """Activate configured client rules after a synchronous strategy order fills."""
        if float(order.filled_quantity or 0.0) <= 0 or self.current_event_time is None:
            return
        self._activate_configured_position(order.asset, self.current_event_time, order=order)
        self._persist()

    async def _activate_untracked_positions(self, timestamp: datetime) -> None:
        positions = await self.broker.get_positions_async()
        for asset in positions:
            self._activate_configured_position(asset, timestamp)

    def _activate_configured_position(
        self,
        asset: str,
        timestamp: datetime,
        *,
        order: Order | None = None,
    ) -> None:
        if asset in self._rule_states:
            return
        rules = self._rules_by_asset.get(asset, self._global_rules)
        position = self._position(asset)
        if rules is None or position is None or position.quantity == 0:
            return
        policy_id = next(
            (
                candidate_id
                for candidate_id, candidate_rules in self._rules_by_policy.items()
                if candidate_rules == rules
            ),
            f"strategy-position-rules:{asset}",
        )
        entry_price = (
            float(order.filled_price or position.entry_price)
            if order
            else float(position.entry_price)
        )
        quantity = abs(float(position.quantity))
        original_entry_time = self._as_utc(position.entry_time)
        self._rule_states[asset] = PositionRuleState(
            policy_id=policy_id,
            rule_id=policy_id,
            asset=asset,
            activation=RuleActivation.ACTIVE,
            entry_time=timestamp,
            entry_side=(SpecOrderSide.BUY if position.quantity > 0 else SpecOrderSide.SELL),
            entry_price=entry_price,
            entry_quantity=quantity,
            high_water_mark=entry_price,
            low_water_mark=entry_price,
            max_favorable_excursion=0.0,
            max_adverse_excursion=0.0,
            remaining_exit_quantity=quantity,
            idempotency_key=f"position:{asset}:{original_entry_time.isoformat()}:rules",
            action=PositionActionType.HOLD,
            exit_reason=ExitReason.NONE,
            evaluation_mode=EvaluationMode.CLIENT,
        )

    async def _process_targets(self, timestamp: datetime, data: dict[str, dict[str, Any]]) -> None:
        for intent in self._targets.values():
            if intent.intent_id in self._processed_targets:
                continue
            if intent.effective_session > timestamp.date():
                continue
            if intent.effective_session < timestamp.date():
                raise LiveIntentError(f"target {intent.intent_id!r} missed its effective session")
            if intent.decision_time >= timestamp or intent.information_cutoff >= timestamp:
                raise LiveIntentError(
                    f"target {intent.intent_id!r} decision and cutoff must precede opening"
                )
            children = await self._lower(intent, data)
            for child in children:
                order = await self._submit_child(child, data[child.asset])
                self._orders[order.order_id] = order
                self._order_by_child[child.child_intent_id] = order.order_id
            self._processed_targets.add(intent.intent_id)
            await self._reconcile(timestamp, target_intent_id=intent.intent_id)

    async def _lower(
        self,
        intent: CanonicalTargetIntent,
        data: dict[str, dict[str, Any]],
    ) -> list[CanonicalChildOrderIntent]:
        positions = await self.broker.get_positions_async()
        equity = await self.broker.get_account_value_async()
        cost_rate = (
            self.policy.fee_bps
            + self.policy.slippage_bps
            + self.policy.spread_bps
            + self.policy.impact_bps
        ) / 10_000
        desired: dict[str, float] = {}
        raw: dict[str, float] = {}
        for target in intent.targets:
            asset_data = data.get(target.asset)
            if asset_data is None:
                raise LiveIntentError(f"opening event has no data for {target.asset}")
            price = self._price(asset_data, "open")
            quantity = (
                equity * (1 - intent.cash_buffer) * target.value / (price * (1 + cost_rate))
                if target.measure is TargetMeasure.WEIGHT and target.value > 0
                else (
                    equity * (1 - intent.cash_buffer) * target.value / price
                    if target.measure is TargetMeasure.WEIGHT
                    else target.value
                )
            )
            raw[target.asset] = quantity
            desired[target.asset] = self._round(quantity, intent.rounding)
        if intent.residual is ResidualPolicy.REJECT and any(
            not math.isclose(raw[asset], desired[asset], abs_tol=1e-12) for asset in raw
        ):
            raise LiveIntentError("target leaves a rounding residual under reject policy")
        children: list[CanonicalChildOrderIntent] = []
        for target in sorted(intent.targets, key=lambda item: item.asset):
            current = positions.get(target.asset)
            delta = desired[target.asset] - (current.quantity if current is not None else 0.0)
            if math.isclose(delta, 0.0, abs_tol=1e-12):
                continue
            child = CanonicalChildOrderIntent(
                child_intent_id=f"{intent.intent_id}:{target.asset}",
                target_intent_id=intent.intent_id,
                idempotency_key=f"{intent.idempotency_key}:{target.asset}",
                asset=target.asset,
                side=SpecOrderSide.BUY if delta > 0 else SpecOrderSide.SELL,
                quantity=abs(delta),
                order_type=SpecOrderType.MARKET,
                parameters=OrderParameters(),
                decision_session=intent.effective_session,
                effective_session=intent.effective_session,
                eligibility_phase=LifecyclePhase.PRE_OPEN,
                fill_eligibility=FillEligibility.OPENING_AUCTION,
                time_in_force=TimeInForce.OPG,
                session_policy=SessionPolicy.REGULAR,
                capabilities=(ExecutionCapability.OPENING_AUCTION,),
                reason=intent.reason,
                lifecycle_version=intent.lifecycle_version,
            )
            self._children[child.child_intent_id] = child
            children.append(child)
        return children

    async def _submit_child(
        self, child: CanonicalChildOrderIntent, asset_data: dict[str, Any]
    ) -> Order:
        if self.policy.opening_auction is not ExecutionBehavior.CLIENT:
            raise UnsupportedLiveCapabilityError(
                "broker-native opening targets must be submitted before the opening event"
            )
        price = self._price(asset_data, "open")
        record = getattr(self.broker, "record_market_snapshot", None)
        if callable(record):
            record(child.asset, price)
        return await self.broker.submit_order_async(
            child.asset,
            child.quantity,
            OrderSide.BUY if child.side is SpecOrderSide.BUY else OrderSide.SELL,
            OrderType.MARKET,
            target_intent_id=child.target_intent_id,
            child_intent_id=child.child_intent_id,
            intent_idempotency_key=child.idempotency_key,
            opening_auction=True,
        )

    async def _reconcile(self, timestamp: datetime, *, target_intent_id: str | None = None) -> None:
        pending = {order.order_id: order for order in await self.broker.get_pending_orders_async()}
        for child in self._children.values():
            if target_intent_id is not None and child.target_intent_id != target_intent_id:
                continue
            order_id = self._order_by_child.get(child.child_intent_id)
            if order_id is None:
                continue
            order = self._orders.get(order_id) or pending.get(order_id)
            if order is None:
                continue
            filled = float(order.filled_quantity or 0.0)
            remaining = child.remaining_after_fill(min(filled, child.quantity))
            if order.status is OrderStatus.REJECTED:
                outcome = IntentOutcome.REJECTED
            elif order.status is OrderStatus.CANCELLED:
                outcome = IntentOutcome.CANCELLED
            elif remaining == 0:
                outcome = IntentOutcome.FULL
            elif filled > 0:
                outcome = IntentOutcome.PARTIAL
            else:
                outcome = IntentOutcome.PENDING
            intent = self._targets[child.target_intent_id]
            activation = self._activate_rule(intent, child, order, timestamp)
            record = IntentReconciliation(
                target_intent_id=child.target_intent_id,
                child_intent_id=child.child_intent_id,
                order_id=order_id,
                event_time=timestamp,
                requested_quantity=child.quantity,
                filled_quantity=filled,
                remaining_quantity=remaining,
                outcome=outcome,
                rejection_reason=order.rejection_reason,
                rule_policy_id=intent.position_rule_policy_id,
                rule_activated_at=activation,
            )
            previous = self._latest_reconciliation.get(child.child_intent_id)
            if previous is None or not self._same_reconciliation_state(previous, record):
                self._reconciliations.append(record)
                self._latest_reconciliation[child.child_intent_id] = record

    def _activate_rule(
        self,
        intent: CanonicalTargetIntent,
        child: CanonicalChildOrderIntent,
        order: Order,
        timestamp: datetime,
    ) -> datetime | None:
        policy_id = intent.position_rule_policy_id
        filled = float(order.filled_quantity or 0.0)
        if policy_id is None or filled <= 0:
            return None
        asset = child.asset
        if asset in self._rule_states:
            previous_fill = self._target_rule_filled.get(child.child_intent_id, filled)
            if filled < previous_fill:
                raise LiveIntentError(
                    f"target fill for {child.child_intent_id!r} decreased across reconciliation"
                )
            if filled > previous_fill:
                state = self._rule_states[asset]
                position = self._position(asset)
                if position is None:
                    raise LiveIntentError(
                        f"target {intent.intent_id!r} has fills but no position for {asset}"
                    )
                added = filled - previous_fill
                current_quantity = abs(float(position.quantity))
                entry_quantity = max(state.entry_quantity, current_quantity)
                remaining = min(entry_quantity, state.remaining_exit_quantity + added)
                self._rule_states[asset] = PositionRuleState(
                    policy_id=state.policy_id,
                    rule_id=state.rule_id,
                    asset=state.asset,
                    activation=state.activation,
                    entry_time=state.entry_time,
                    entry_side=state.entry_side,
                    entry_price=float(position.entry_price),
                    entry_quantity=entry_quantity,
                    high_water_mark=max(state.high_water_mark, float(position.entry_price)),
                    low_water_mark=min(state.low_water_mark, float(position.entry_price)),
                    max_favorable_excursion=state.max_favorable_excursion,
                    max_adverse_excursion=state.max_adverse_excursion,
                    remaining_exit_quantity=remaining,
                    idempotency_key=state.idempotency_key,
                    action=state.action,
                    exit_reason=state.exit_reason,
                    evaluation_mode=state.evaluation_mode,
                    action_quantity=state.action_quantity,
                    adjusted_stop_price=state.adjusted_stop_price,
                    lifecycle_version=state.lifecycle_version,
                )
                self._target_rule_filled[child.child_intent_id] = filled
            return self._rule_states[asset].entry_time
        position = self._position(asset)
        if position is None:
            return None
        entry_time = timestamp
        entry_price = float(order.filled_price or position.entry_price)
        quantity = abs(float(position.quantity))
        self._rule_states[asset] = PositionRuleState(
            policy_id=policy_id,
            rule_id=policy_id,
            asset=asset,
            activation=RuleActivation.ACTIVE,
            entry_time=entry_time,
            entry_side=child.side,
            entry_price=entry_price,
            entry_quantity=quantity,
            high_water_mark=entry_price,
            low_water_mark=entry_price,
            max_favorable_excursion=0.0,
            max_adverse_excursion=0.0,
            remaining_exit_quantity=quantity,
            idempotency_key=f"{intent.idempotency_key}:{asset}:rules",
            action=PositionActionType.HOLD,
            exit_reason=ExitReason.NONE,
            evaluation_mode=EvaluationMode.CLIENT,
        )
        self._target_rule_filled[child.child_intent_id] = filled
        self._rules_by_asset[asset] = self._rules_by_policy[policy_id]
        return entry_time

    async def _evaluate_rules(
        self,
        timestamp: datetime,
        data: dict[str, dict[str, Any]],
        context: dict[str, Any],
        *,
        skip_assets: set[str],
    ) -> None:
        for asset, state in tuple(self._rule_states.items()):
            if asset in skip_assets or state.activation is RuleActivation.COMPLETE:
                continue
            position = self._position(asset)
            asset_data = data.get(asset)
            rules = self._rules_by_asset.get(asset, self._global_rules)
            if position is None or asset_data is None or rules is None:
                continue
            if state.entry_time == timestamp:
                continue
            close = self._price(asset_data, "close")
            high = self._price(asset_data, "high", default=close)
            low = self._price(asset_data, "low", default=close)
            open_price = self._price(asset_data, "open", default=close)
            position.current_price = close
            raw_return = (close - state.entry_price) / state.entry_price
            unrealized_return = raw_return if position.quantity > 0 else -raw_return
            merged_context = dict(position.context)
            merged_context.update(self._rule_context.get(asset, {}))
            asset_context = context.get(asset, {}) if isinstance(context, dict) else {}
            if isinstance(asset_context, dict):
                merged_context.update(asset_context)
            position_state = PositionState(
                asset=asset,
                side="long" if position.quantity > 0 else "short",
                entry_price=state.entry_price,
                current_price=close,
                quantity=abs(position.quantity),
                initial_quantity=state.entry_quantity,
                unrealized_pnl=(close - state.entry_price) * position.quantity,
                unrealized_return=unrealized_return,
                bars_held=self._duration_events.get(asset, 0),
                high_water_mark=state.high_water_mark,
                low_water_mark=state.low_water_mark,
                bar_open=open_price,
                bar_high=high,
                bar_low=low,
                max_favorable_excursion=state.max_favorable_excursion,
                max_adverse_excursion=state.max_adverse_excursion,
                entry_time=state.entry_time,
                current_time=timestamp,
                context=merged_context,
            )
            action = rules.evaluate(position_state)
            high_water = max(state.high_water_mark, high)
            low_water = min(state.low_water_mark, low)
            if position.quantity > 0:
                favorable_return = (high - state.entry_price) / state.entry_price
                adverse_return = (low - state.entry_price) / state.entry_price
            else:
                favorable_return = (state.entry_price - low) / state.entry_price
                adverse_return = (state.entry_price - high) / state.entry_price
            favorable = max(state.max_favorable_excursion, favorable_return)
            adverse = min(state.max_adverse_excursion, adverse_return)
            self._duration_events[asset] = self._duration_events.get(asset, 0) + 1
            if action.action is ActionType.HOLD:
                self._rule_states[asset] = self._updated_rule_state(
                    state,
                    high_water=high_water,
                    low_water=low_water,
                    favorable=favorable,
                    adverse=adverse,
                )
                continue
            if action.action not in {ActionType.EXIT_FULL, ActionType.EXIT_PARTIAL}:
                raise UnsupportedLiveCapabilityError(
                    f"client position action {action.action.name.lower()} is not implemented"
                )
            quantity = (
                abs(position.quantity)
                if action.action is ActionType.EXIT_FULL
                else abs(position.quantity) * action.pct
            )
            exit_key = (
                f"{state.idempotency_key}:exit:{action.action.name.lower()}:"
                f"{state.remaining_exit_quantity:.12g}:{action.pct:.12g}"
            )
            if exit_key in self._exit_idempotency:
                continue
            reducer = getattr(self.broker, "reduce_position_async", None)
            if not callable(reducer):
                raise UnsupportedLiveCapabilityError(
                    "client position rules require SafeBroker.reduce_position_async"
                )
            try:
                order = await reducer(
                    asset,
                    quantity,
                    reason=action.reason,
                    idempotency_key=exit_key,
                    fill_price=action.fill_price,
                )
                if order.status in {OrderStatus.REJECTED, OrderStatus.CANCELLED}:
                    detail = order.rejection_reason or order.status.value
                    raise ReducingRiskExecutionError(
                        f"reducing-risk order was not accepted for {asset}: {detail}"
                    )
            except Exception as error:
                if self._halt_on_reducing_risk_failure():
                    raise ReducingRiskExecutionError(
                        f"reducing-risk execution failed for {asset}: "
                        f"{redact_sensitive(str(error))}"
                    ) from None
                continue
            self._exit_idempotency.add(exit_key)
            reason = self._exit_reason(action.reason)
            typed_action = (
                PositionActionType.EXIT_FULL
                if action.action is ActionType.EXIT_FULL
                else PositionActionType.EXIT_PARTIAL
            )
            self._orders[order.order_id] = order
            self._rule_exit_orders[exit_key] = {
                "asset": asset,
                "order_id": order.order_id,
                "requested_quantity": quantity,
                "action": typed_action.value,
                "reason": reason.value,
            }
            self._rule_exit_filled.setdefault(exit_key, 0.0)
            self._rule_states[asset] = self._rule_state_after_exit_fill(
                state,
                filled_delta=self._new_rule_exit_fill(exit_key, order),
                reason=reason,
                high_water=high_water,
                low_water=low_water,
                favorable=favorable,
                adverse=adverse,
            )

    async def _reconcile_rule_exits(self) -> set[str]:
        filled_assets: set[str] = set()
        pending = {order.order_id: order for order in await self.broker.get_pending_orders_async()}
        for exit_key, metadata in tuple(self._rule_exit_orders.items()):
            asset = str(metadata["asset"])
            state = self._rule_states.get(asset)
            if state is None or state.activation is RuleActivation.COMPLETE:
                continue
            order_id = str(metadata["order_id"])
            order = self._orders.get(order_id) or pending.get(order_id)
            if order is None:
                error = ReducingRiskExecutionError(
                    f"accepted reducing-risk order {order_id!r} for {asset} is unavailable"
                )
                if self._halt_on_reducing_risk_failure():
                    raise error
                continue
            if order.status in {OrderStatus.REJECTED, OrderStatus.CANCELLED}:
                detail = order.rejection_reason or order.status.value
                self._exit_idempotency.discard(exit_key)
                del self._rule_exit_orders[exit_key]
                self._rule_exit_filled.pop(exit_key, None)
                if self._halt_on_reducing_risk_failure():
                    raise ReducingRiskExecutionError(
                        f"reducing-risk order {order_id!r} for {asset} ended as {detail}"
                    )
                continue
            filled_delta = self._new_rule_exit_fill(exit_key, order)
            if filled_delta == 0:
                continue
            filled_assets.add(asset)
            self._rule_states[asset] = self._rule_state_after_exit_fill(
                state,
                filled_delta=filled_delta,
                reason=ExitReason(str(metadata["reason"])),
                high_water=state.high_water_mark,
                low_water=state.low_water_mark,
                favorable=state.max_favorable_excursion,
                adverse=state.max_adverse_excursion,
            )
        return filled_assets

    def _new_rule_exit_fill(self, exit_key: str, order: Order) -> float:
        requested = float(self._rule_exit_orders[exit_key]["requested_quantity"])
        current = float(order.filled_quantity or 0.0)
        previous = self._rule_exit_filled.get(exit_key, 0.0)
        if not 0 <= previous <= current <= requested:
            raise ReducingRiskExecutionError(
                f"invalid cumulative fill {current:g} for reducing-risk intent {exit_key!r}"
            )
        self._rule_exit_filled[exit_key] = current
        return current - previous

    @staticmethod
    def _rule_state_after_exit_fill(
        state: PositionRuleState,
        *,
        filled_delta: float,
        reason: ExitReason,
        high_water: float,
        low_water: float,
        favorable: float,
        adverse: float,
    ) -> PositionRuleState:
        if filled_delta == 0:
            return PositionRuleState(
                policy_id=state.policy_id,
                rule_id=state.rule_id,
                asset=state.asset,
                activation=state.activation,
                entry_time=state.entry_time,
                entry_side=state.entry_side,
                entry_price=state.entry_price,
                entry_quantity=state.entry_quantity,
                high_water_mark=high_water,
                low_water_mark=low_water,
                max_favorable_excursion=favorable,
                max_adverse_excursion=adverse,
                remaining_exit_quantity=state.remaining_exit_quantity,
                idempotency_key=state.idempotency_key,
                action=state.action,
                exit_reason=state.exit_reason,
                evaluation_mode=state.evaluation_mode,
                action_quantity=state.action_quantity,
                adjusted_stop_price=state.adjusted_stop_price,
                lifecycle_version=state.lifecycle_version,
            )
        remaining = max(0.0, state.remaining_exit_quantity - filled_delta)
        completed = remaining == 0
        recorded_action = (
            PositionActionType.EXIT_FULL if completed else PositionActionType.EXIT_PARTIAL
        )
        return PositionRuleState(
            policy_id=state.policy_id,
            rule_id=state.rule_id,
            asset=state.asset,
            activation=RuleActivation.COMPLETE if completed else RuleActivation.TRIGGERED,
            entry_time=state.entry_time,
            entry_side=state.entry_side,
            entry_price=state.entry_price,
            entry_quantity=state.entry_quantity,
            high_water_mark=high_water,
            low_water_mark=low_water,
            max_favorable_excursion=favorable,
            max_adverse_excursion=adverse,
            remaining_exit_quantity=remaining,
            idempotency_key=state.idempotency_key,
            action=recorded_action,
            exit_reason=reason,
            evaluation_mode=EvaluationMode.CLIENT,
            action_quantity=None if completed else filled_delta,
            lifecycle_version=state.lifecycle_version,
        )

    def _halt_on_reducing_risk_failure(self) -> bool:
        return bool(
            getattr(getattr(self.broker, "config", None), "halt_on_reducing_risk_failure", True)
        )

    def _updated_rule_state(
        self,
        state: PositionRuleState,
        *,
        high_water: float,
        low_water: float,
        favorable: float,
        adverse: float,
    ) -> PositionRuleState:
        return PositionRuleState(
            policy_id=state.policy_id,
            rule_id=state.rule_id,
            asset=state.asset,
            activation=state.activation,
            entry_time=state.entry_time,
            entry_side=state.entry_side,
            entry_price=state.entry_price,
            entry_quantity=state.entry_quantity,
            high_water_mark=high_water,
            low_water_mark=low_water,
            max_favorable_excursion=favorable,
            max_adverse_excursion=adverse,
            remaining_exit_quantity=state.remaining_exit_quantity,
            idempotency_key=state.idempotency_key,
            action=state.action,
            exit_reason=state.exit_reason,
            evaluation_mode=state.evaluation_mode,
            action_quantity=state.action_quantity,
            adjusted_stop_price=state.adjusted_stop_price,
            lifecycle_version=state.lifecycle_version,
        )

    def _validate_preconnect_capabilities(self) -> None:
        capabilities = frozenset(getattr(self.broker, "execution_capabilities", ()))
        if (
            self.policy.opening_auction is ExecutionBehavior.BROKER_NATIVE
            and ExecutionCapability.OPENING_AUCTION not in capabilities
        ):
            raise UnsupportedLiveCapabilityError(
                "opening_auction broker-native capability is not declared by the venue"
            )
        if self.policy.contingent is ExecutionBehavior.BROKER_NATIVE:
            raise UnsupportedLiveCapabilityError(
                "broker-native contingent position rules are not implemented"
            )

    def _position(self, asset: str):
        getter = getattr(self.broker, "get_position", None)
        return getter(asset) if callable(getter) else None

    def _persist(self) -> None:
        save = getattr(self.broker, "save_portable_strategy_state", None)
        if callable(save):
            save(self.to_state())

    def to_state(self) -> dict[str, Any]:
        return {
            "targets": [intent.to_dict() for intent in self.targets],
            "children": [child.to_dict() for child in self.children],
            "order_by_child": dict(self._order_by_child),
            "processed_targets": sorted(self._processed_targets),
            "reconciliations": [record.to_dict() for record in self._reconciliations],
            "position_rule_states": [
                {
                    **state.to_dict(),
                    "duration_events": self._duration_events.get(state.asset, 0),
                    "context": self._rule_context.get(state.asset, {}),
                }
                for state in self.position_rule_states
            ],
            "exit_idempotency": sorted(self._exit_idempotency),
            "rule_exit_orders": self._rule_exit_orders,
            "rule_exit_filled": self._rule_exit_filled,
            "target_rule_filled": self._target_rule_filled,
        }

    def _restore(self) -> None:
        load = getattr(self.broker, "load_portable_strategy_state", None)
        if not callable(load):
            return
        state = load()
        for raw in state.get("targets", ()):
            intent = CanonicalTargetIntent.from_mapping(raw)
            self._targets[intent.intent_id] = intent
            self._idempotency[intent.idempotency_key] = intent.intent_id
        for raw in state.get("children", ()):
            child = CanonicalChildOrderIntent.from_mapping(raw)
            self._children[child.child_intent_id] = child
        self._order_by_child = dict(state.get("order_by_child", {}))
        self._processed_targets = set(state.get("processed_targets", ()))
        self._reconciliations = [
            IntentReconciliation.from_mapping(raw) for raw in state.get("reconciliations", ())
        ]
        self._latest_reconciliation = {
            record.child_intent_id: record for record in self._reconciliations
        }
        for raw in state.get("position_rule_states", ()):
            rule_state = PositionRuleState.from_mapping(raw)
            self._rule_states[rule_state.asset] = rule_state
            self._duration_events[rule_state.asset] = int(raw.get("duration_events", 0))
            self._rule_context[rule_state.asset] = dict(raw.get("context", {}))
        self._exit_idempotency = set(state.get("exit_idempotency", ()))
        self._rule_exit_orders = {
            str(key): dict(value) for key, value in state.get("rule_exit_orders", {}).items()
        }
        self._rule_exit_filled = {
            str(key): float(value) for key, value in state.get("rule_exit_filled", {}).items()
        }
        self._target_rule_filled = {
            str(key): float(value) for key, value in state.get("target_rule_filled", {}).items()
        }

    @staticmethod
    def _round(value: float, policy: RoundingPolicy) -> float:
        if policy is RoundingPolicy.NONE:
            return value
        if policy is RoundingPolicy.TOWARD_ZERO:
            return float(math.trunc(value))
        return math.copysign(float(math.floor(abs(value) + 0.5)), value)

    @staticmethod
    def _same_reconciliation_state(
        previous: IntentReconciliation,
        current: IntentReconciliation,
    ) -> bool:
        return (
            previous.target_intent_id == current.target_intent_id
            and previous.child_intent_id == current.child_intent_id
            and previous.order_id == current.order_id
            and previous.requested_quantity == current.requested_quantity
            and previous.filled_quantity == current.filled_quantity
            and previous.remaining_quantity == current.remaining_quantity
            and previous.outcome is current.outcome
            and previous.rejection_reason == current.rejection_reason
            and previous.rule_policy_id == current.rule_policy_id
            and previous.rule_activated_at == current.rule_activated_at
        )

    @staticmethod
    def _price(data: dict[str, Any], field: str, *, default: float | None = None) -> float:
        value = data.get(field, default)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise LiveIntentError(f"{field} price must be numeric")
        price = float(value)
        if not math.isfinite(price) or price <= 0:
            raise LiveIntentError(f"{field} price must be finite and positive")
        return price

    @staticmethod
    def _exit_reason(reason: str) -> ExitReason:
        if reason.startswith("stop_loss"):
            return ExitReason.STOP_LOSS
        if reason.startswith("take_profit"):
            return ExitReason.TAKE_PROFIT
        if reason.startswith("trailing_stop"):
            return ExitReason.TRAILING_STOP
        if reason.startswith("time_exit"):
            return ExitReason.TIME_EXIT
        return ExitReason.SIGNAL

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


__all__ = [
    "LiveIntentError",
    "LiveStrategyRuntime",
    "LiveStrategyRuntimeError",
    "ReducingRiskExecutionError",
    "UnsupportedLiveCapabilityError",
    "default_live_execution_policy",
]
