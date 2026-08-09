"""Validated migration for persisted portable strategy state."""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any

from ml4t.backtest import IntentReconciliation
from ml4t.specs import (
    CanonicalChildOrderIntent,
    CanonicalTargetIntent,
    LifecyclePhase,
    PositionActionType,
    PositionRuleState,
    RuleActivation,
)

from .persistence import CorruptStateError

PORTABLE_STRATEGY_STATE_SCHEMA_VERSION = 1
_PORTABLE_FIELDS = {
    "schema_version",
    "targets",
    "children",
    "order_by_child",
    "processed_targets",
    "reconciliations",
    "position_rule_states",
    "exit_idempotency",
    "rule_exit_orders",
    "rule_exit_filled",
    "target_rule_filled",
}


def migrate_portable_strategy_state(
    value: dict[str, Any],
    *,
    position_quantities: dict[str, float],
) -> tuple[dict[str, Any], bool]:
    """Validate current state or upgrade the unversioned 0.1.0b4 representation."""
    if not value:
        return {}, False
    original = deepcopy(value)
    try:
        migrated = _migrate_portable_strategy_state(value, position_quantities)
    except CorruptStateError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise CorruptStateError("portable strategy state is invalid") from error
    return migrated, migrated != original


def _migrate_portable_strategy_state(
    value: dict[str, Any], position_quantities: dict[str, float]
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CorruptStateError("portable strategy state must be an object")
    unknown = set(value) - _PORTABLE_FIELDS
    if unknown:
        raise CorruptStateError(
            f"portable strategy state contains unsupported fields: {sorted(unknown)}"
        )
    version = value.get("schema_version")
    if version not in {None, PORTABLE_STRATEGY_STATE_SCHEMA_VERSION}:
        raise CorruptStateError(f"unsupported portable strategy state schema: {version!r}")
    legacy = version is None

    targets = [CanonicalTargetIntent.from_mapping(raw) for raw in _record_list(value, "targets")]
    targets_by_id = {target.intent_id: target for target in targets}
    if len(targets_by_id) != len(targets):
        raise CorruptStateError("portable strategy state contains duplicate target intents")

    children = []
    child_sides: dict[str, set[str]] = {}
    for raw in _record_list(value, "children"):
        child_value = dict(raw)
        if legacy and (
            "decision_session" not in child_value or "effective_session" not in child_value
        ):
            target = targets_by_id.get(str(child_value.get("target_intent_id")))
            if target is None:
                raise CorruptStateError("legacy child intent has no matching target intent")
            child_value["decision_session"] = target.effective_session.isoformat()
            child_value["effective_session"] = target.effective_session.isoformat()
            child_value["eligibility_phase"] = LifecyclePhase.PRE_OPEN.value
        child = CanonicalChildOrderIntent.from_mapping(child_value)
        children.append(child)
        child_sides.setdefault(child.asset, set()).add(child.side.value)

    reconciliations = [
        IntentReconciliation.from_mapping(raw) for raw in _record_list(value, "reconciliations")
    ]
    rule_states = [
        _migrate_rule_state(raw, position_quantities, child_sides, legacy=legacy)
        for raw in _record_list(value, "position_rule_states")
    ]

    result = {
        "schema_version": PORTABLE_STRATEGY_STATE_SCHEMA_VERSION,
        "targets": [target.to_dict() for target in targets],
        "children": [child.to_dict() for child in children],
        "order_by_child": _string_mapping(value, "order_by_child"),
        "processed_targets": _string_list(value, "processed_targets"),
        "reconciliations": [record.to_dict() for record in reconciliations],
        "position_rule_states": rule_states,
        "exit_idempotency": _string_list(value, "exit_idempotency"),
        "rule_exit_orders": _mapping(value, "rule_exit_orders"),
        "rule_exit_filled": _number_mapping(value, "rule_exit_filled"),
        "target_rule_filled": _number_mapping(value, "target_rule_filled"),
    }
    return result


def _migrate_rule_state(
    raw: dict[str, Any],
    position_quantities: dict[str, float],
    child_sides: dict[str, set[str]],
    *,
    legacy: bool,
) -> dict[str, Any]:
    state = dict(raw)
    if legacy:
        state.setdefault("rule_id", state.get("policy_id"))
    asset = str(state.get("asset", ""))
    if legacy and "entry_side" not in state:
        quantity = position_quantities.get(asset, 0.0)
        if quantity:
            state["entry_side"] = "buy" if quantity > 0 else "sell"
        elif len(child_sides.get(asset, set())) == 1:
            state["entry_side"] = next(iter(child_sides[asset]))
        else:
            raise CorruptStateError(
                f"legacy position-rule state cannot infer entry side for {asset!r}"
            )

    if legacy:
        action = PositionActionType(state["action"])
        entry_quantity = float(state["entry_quantity"])
        remaining = float(state["remaining_exit_quantity"])
        executed = max(0.0, entry_quantity - remaining)
        if action in {PositionActionType.EXIT_FULL, PositionActionType.EXIT_PARTIAL}:
            if remaining == 0:
                state["activation"] = RuleActivation.COMPLETE.value
                state["action"] = PositionActionType.EXIT_FULL.value
                state["action_quantity"] = None
            elif executed > 0:
                state["activation"] = RuleActivation.TRIGGERED.value
                state["action"] = PositionActionType.EXIT_PARTIAL.value
                state["action_quantity"] = executed
            else:
                state["activation"] = RuleActivation.ACTIVE.value
                state["action"] = PositionActionType.HOLD.value
                state["exit_reason"] = "none"
                state["action_quantity"] = None
        else:
            state.setdefault("action_quantity", None)
        state.setdefault("adjusted_stop_price", None)

    duration_events = state.pop("duration_events", 0)
    context = state.pop("context", {})
    if (
        isinstance(duration_events, bool)
        or not isinstance(duration_events, int)
        or duration_events < 0
    ):
        raise CorruptStateError("position-rule duration_events must be a non-negative integer")
    if not isinstance(context, dict):
        raise CorruptStateError("position-rule context must be an object")
    normalized = PositionRuleState.from_mapping(state).to_dict()
    normalized["duration_events"] = duration_events
    normalized["context"] = deepcopy(context)
    return normalized


def _record_list(value: dict[str, Any], name: str) -> list[dict[str, Any]]:
    records = value.get(name, [])
    if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
        raise CorruptStateError(f"portable strategy state {name} must be a list of objects")
    return records


def _string_list(value: dict[str, Any], name: str) -> list[str]:
    items = value.get(name, [])
    if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
        raise CorruptStateError(f"portable strategy state {name} must be a list of strings")
    return list(items)


def _mapping(value: dict[str, Any], name: str) -> dict[str, Any]:
    mapping = value.get(name, {})
    if not isinstance(mapping, dict) or any(not isinstance(key, str) for key in mapping):
        raise CorruptStateError(f"portable strategy state {name} must be an object")
    return deepcopy(mapping)


def _string_mapping(value: dict[str, Any], name: str) -> dict[str, str]:
    mapping = _mapping(value, name)
    if any(not isinstance(item, str) for item in mapping.values()):
        raise CorruptStateError(f"portable strategy state {name} values must be strings")
    return mapping


def _number_mapping(value: dict[str, Any], name: str) -> dict[str, float]:
    mapping = _mapping(value, name)
    if any(
        isinstance(item, bool)
        or not isinstance(item, int | float)
        or not math.isfinite(float(item))
        for item in mapping.values()
    ):
        raise CorruptStateError(f"portable strategy state {name} values must be numeric")
    return {key: float(item) for key, item in mapping.items()}
