"""Unit tests for LiveRiskConfig and RiskState.

Tests cover:
- Configuration validation (invalid values raise ValueError)
- Default values
- RiskState serialization (to_dict/from_dict)
- Atomic file operations (save_atomic)
"""

import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from ml4t.live.persistence import CorruptStateError
from ml4t.live.safety import ExecutionMode, ExecutionModeError, LiveRiskConfig, RiskState

# === LiveRiskConfig Tests ===


def test_live_risk_config_defaults():
    """Test default configuration values."""
    config = LiveRiskConfig()

    # Position limits
    assert config.max_position_value == 50_000.0
    assert config.max_position_shares == 1000
    assert config.max_total_exposure == 200_000.0
    assert config.max_positions == 20

    # Order limits
    assert config.max_order_value == 10_000.0
    assert config.max_order_shares == 500
    assert config.max_orders_per_minute == 10

    # Loss limits
    assert config.max_daily_loss == 5_000.0
    assert config.max_drawdown_pct == 0.05

    # Price protection
    assert config.max_price_deviation_pct == 0.05
    assert config.max_data_staleness_seconds == 60.0
    assert config.dedup_window_seconds == 1.0

    # Asset restrictions
    assert config.allowed_assets == set()
    assert config.blocked_assets == set()

    # Flags
    assert config.shadow_mode is False
    assert config.execution_mode is None
    assert config.kill_switch_enabled is False

    # State file
    assert config.state_file == ".ml4t_risk_state.json"


def test_live_risk_config_custom_values():
    """Test creating config with custom values."""
    config = LiveRiskConfig(
        max_position_value=25_000.0,
        max_daily_loss=2_000.0,
        shadow_mode=True,
        allowed_assets={"AAPL", "TSLA"},
    )

    assert config.max_position_value == 25_000.0
    assert config.max_daily_loss == 2_000.0
    assert config.shadow_mode is True
    assert config.allowed_assets == {"AAPL", "TSLA"}
    assert config.execution_mode is ExecutionMode.SHADOW


@pytest.mark.parametrize("mode", list(ExecutionMode))
def test_live_risk_config_normalizes_explicit_execution_mode(mode):
    config = LiveRiskConfig(execution_mode=mode.value.upper())

    assert config.execution_mode is mode
    assert config.shadow_mode is (mode is ExecutionMode.SHADOW)
    assert config.require_execution_mode() is mode


def test_live_risk_config_rejects_ambiguous_or_conflicting_execution_mode():
    with pytest.raises(ExecutionModeError, match="shadow_mode=False is ambiguous"):
        LiveRiskConfig(shadow_mode=False).require_execution_mode()

    with pytest.raises(ExecutionModeError, match="must be one of"):
        LiveRiskConfig(execution_mode="external")

    with pytest.raises(ExecutionModeError, match="conflicts"):
        LiveRiskConfig(shadow_mode=True, execution_mode="paper")


def test_live_risk_config_accepts_fractional_share_limits():
    config = LiveRiskConfig(max_position_shares=2.5, max_order_shares=0.25)

    assert config.max_position_shares == 2.5
    assert config.max_order_shares == 0.25


def test_live_risk_config_validation_position_limits():
    """Test validation of position limit parameters."""
    # Negative max_position_value
    with pytest.raises(ValueError, match="max_position_value must be positive"):
        LiveRiskConfig(max_position_value=-1000.0)

    # Zero max_position_value
    with pytest.raises(ValueError, match="max_position_value must be positive"):
        LiveRiskConfig(max_position_value=0.0)

    # Negative max_position_shares
    with pytest.raises(ValueError, match="max_position_shares must be positive"):
        LiveRiskConfig(max_position_shares=-100)

    # Negative max_total_exposure
    with pytest.raises(ValueError, match="max_total_exposure must be positive"):
        LiveRiskConfig(max_total_exposure=-50000.0)

    # Negative max_positions
    with pytest.raises(ValueError, match="max_positions must be positive"):
        LiveRiskConfig(max_positions=-5)


def test_live_risk_config_validation_order_limits():
    """Test validation of order limit parameters."""
    # Negative max_order_value
    with pytest.raises(ValueError, match="max_order_value must be positive"):
        LiveRiskConfig(max_order_value=-5000.0)

    # Negative max_order_shares
    with pytest.raises(ValueError, match="max_order_shares must be positive"):
        LiveRiskConfig(max_order_shares=-100)

    # Negative max_orders_per_minute
    with pytest.raises(ValueError, match="max_orders_per_minute must be positive"):
        LiveRiskConfig(max_orders_per_minute=-5)


def test_live_risk_config_validation_loss_limits():
    """Test validation of loss limit parameters."""
    # Negative max_daily_loss
    with pytest.raises(ValueError, match="max_daily_loss must be positive"):
        LiveRiskConfig(max_daily_loss=-1000.0)

    # max_drawdown_pct out of range (too low)
    with pytest.raises(ValueError, match="max_drawdown_pct must be between 0 and 1"):
        LiveRiskConfig(max_drawdown_pct=0.0)

    # max_drawdown_pct out of range (too high)
    with pytest.raises(ValueError, match="max_drawdown_pct must be between 0 and 1"):
        LiveRiskConfig(max_drawdown_pct=1.5)

    # max_drawdown_pct negative
    with pytest.raises(ValueError, match="max_drawdown_pct must be between 0 and 1"):
        LiveRiskConfig(max_drawdown_pct=-0.1)


def test_live_risk_config_validation_price_protection():
    """Test validation of price protection parameters."""
    # max_price_deviation_pct out of range
    with pytest.raises(ValueError, match="max_price_deviation_pct must be between 0 and 1"):
        LiveRiskConfig(max_price_deviation_pct=0.0)

    with pytest.raises(ValueError, match="max_price_deviation_pct must be between 0 and 1"):
        LiveRiskConfig(max_price_deviation_pct=1.5)

    # Negative max_data_staleness_seconds
    with pytest.raises(ValueError, match="max_data_staleness_seconds must be positive"):
        LiveRiskConfig(max_data_staleness_seconds=-10.0)

    # Negative dedup_window_seconds
    with pytest.raises(ValueError, match="dedup_window_seconds must be non-negative"):
        LiveRiskConfig(dedup_window_seconds=-1.0)


def test_live_risk_config_validation_asset_restrictions():
    """Test validation of asset restriction parameters."""
    # Overlap between allowed and blocked assets
    with pytest.raises(ValueError, match="Assets cannot be in both allowed and blocked"):
        LiveRiskConfig(allowed_assets={"AAPL", "TSLA"}, blocked_assets={"AAPL", "MSFT"})

    # No overlap is OK
    config = LiveRiskConfig(allowed_assets={"AAPL", "TSLA"}, blocked_assets={"MSFT", "GOOGL"})
    assert config.allowed_assets == {"AAPL", "TSLA"}
    assert config.blocked_assets == {"MSFT", "GOOGL"}


def test_live_risk_config_validation_state_file():
    """Test validation of state file parameter."""
    # Empty state_file
    with pytest.raises(ValueError, match="state_file cannot be empty"):
        LiveRiskConfig(state_file="")

    with pytest.raises(ValueError, match="must not overlap"):
        LiveRiskConfig(state_file="state.json", journal_file="state.json")

    with pytest.raises(ValueError, match="must not overlap"):
        LiveRiskConfig(state_file="state.json", journal_file="state.json.lock")

    with pytest.raises(ValueError, match="journal_file cannot be empty"):
        LiveRiskConfig(journal_file="")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("max_position_value", True),
        ("max_position_value", "unlimited"),
        ("max_positions", True),
        ("max_positions", 1.5),
    ],
)
def test_live_risk_config_rejects_values_that_only_look_numeric(name, value):
    with pytest.raises(ValueError, match=name):
        LiveRiskConfig(**{name: value})


def test_require_execution_mode_revalidates_mutated_configuration():
    config = LiveRiskConfig(execution_mode="paper")
    config.shadow_mode = True
    with pytest.raises(ExecutionModeError, match="conflicts"):
        config.require_execution_mode()

    config.shadow_mode = False
    config.execution_mode = "external"
    with pytest.raises(ExecutionModeError, match="must be one of"):
        config.require_execution_mode()


def test_live_risk_config_none_disables_limits_explicitly():
    """Test that None is the only non-finite limit representation."""
    config = LiveRiskConfig(
        max_position_value=None,
        max_position_shares=None,
        max_total_exposure=None,
        max_positions=None,
        max_order_value=None,
        max_order_shares=None,
        max_orders_per_minute=None,
        max_daily_loss=None,
        max_drawdown_pct=None,
        max_price_deviation_pct=None,
        max_data_staleness_seconds=None,
        dedup_window_seconds=None,
    )

    assert all(
        value is None
        for value in (
            config.max_position_value,
            config.max_position_shares,
            config.max_total_exposure,
            config.max_positions,
            config.max_order_value,
            config.max_order_shares,
            config.max_orders_per_minute,
            config.max_daily_loss,
            config.max_drawdown_pct,
            config.max_price_deviation_pct,
            config.max_data_staleness_seconds,
            config.dedup_window_seconds,
        )
    )


@pytest.mark.parametrize(
    "name",
    [
        "max_position_value",
        "max_position_shares",
        "max_total_exposure",
        "max_positions",
        "max_order_value",
        "max_order_shares",
        "max_orders_per_minute",
        "max_daily_loss",
        "max_drawdown_pct",
        "max_price_deviation_pct",
        "max_data_staleness_seconds",
        "dedup_window_seconds",
    ],
)
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_live_risk_config_rejects_every_non_finite_numeric_field(name, value):
    """Test that no non-finite value can disable a safety check implicitly."""
    with pytest.raises(ValueError, match=name):
        LiveRiskConfig(**{name: value})


# === RiskState Tests ===


def test_risk_state_defaults():
    """Test RiskState with default values."""
    state = RiskState(date="2023-10-15")

    assert state.date == "2023-10-15"
    assert state.daily_loss == 0.0
    assert state.orders_placed == 0
    assert state.high_water_mark == 0.0
    assert state.kill_switch_activated is False
    assert state.kill_switch_reason == ""


def test_risk_state_custom_values():
    """Test RiskState with custom values."""
    state = RiskState(
        date="2023-10-15",
        daily_loss=1500.0,
        orders_placed=25,
        high_water_mark=105000.0,
        kill_switch_activated=True,
        kill_switch_reason="Max daily loss exceeded",
    )

    assert state.date == "2023-10-15"
    assert state.daily_loss == 1500.0
    assert state.orders_placed == 25
    assert state.high_water_mark == 105000.0
    assert state.kill_switch_activated is True
    assert state.kill_switch_reason == "Max daily loss exceeded"


def test_risk_state_to_dict():
    """Test RiskState serialization to dict."""
    state = RiskState(
        date="2023-10-15", daily_loss=1500.0, orders_placed=25, high_water_mark=105000.0
    )

    data = state.to_dict()
    assert data == {
        "date": "2023-10-15",
        "daily_loss": 1500.0,
        "orders_placed": 25,
        "high_water_mark": 105000.0,
        "kill_switch_activated": False,
        "kill_switch_reason": "",
    }


def test_risk_state_from_dict():
    """Test RiskState deserialization from dict."""
    data = {
        "date": "2023-10-15",
        "daily_loss": 1500.0,
        "orders_placed": 25,
        "high_water_mark": 105000.0,
        "kill_switch_activated": True,
        "kill_switch_reason": "Test reason",
    }

    state = RiskState.from_dict(data)
    assert state.date == "2023-10-15"
    assert state.daily_loss == 1500.0
    assert state.orders_placed == 25
    assert state.high_water_mark == 105000.0
    assert state.kill_switch_activated is True
    assert state.kill_switch_reason == "Test reason"


def test_risk_state_roundtrip():
    """Test to_dict → from_dict roundtrip."""
    original = RiskState(
        date="2023-10-15", daily_loss=1500.0, orders_placed=25, high_water_mark=105000.0
    )

    data = original.to_dict()
    restored = RiskState.from_dict(data)

    assert restored.date == original.date
    assert restored.daily_loss == original.daily_loss
    assert restored.orders_placed == original.orders_placed
    assert restored.high_water_mark == original.high_water_mark
    assert restored.kill_switch_activated == original.kill_switch_activated
    assert restored.kill_switch_reason == original.kill_switch_reason


def valid_state_data(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "date": "2026-08-09",
        "daily_loss": 0.0,
        "orders_placed": 0,
        "high_water_mark": 0.0,
        "kill_switch_activated": False,
        "kill_switch_reason": "",
    }
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "must be an object"),
        (valid_state_data(unknown=True), "unsupported fields"),
        (valid_state_data(date="not-a-date"), "ISO calendar date"),
        (valid_state_data(daily_loss=-1), "non-negative"),
        (valid_state_data(orders_placed=True), "orders_placed"),
        (valid_state_data(orders_placed=-1), "orders_placed"),
        (valid_state_data(kill_switch_activated="yes"), "kill-switch"),
        (valid_state_data(execution_mode="external"), "execution_mode"),
        (valid_state_data(persisted_positions=[]), "persisted_positions"),
        (valid_state_data(persisted_positions={"": 1}), "non-empty strings"),
        (valid_state_data(persisted_positions={"SPY": float("inf")}), "must be finite"),
        (valid_state_data(persisted_pending_orders={}), "pending_orders must be a list"),
        (valid_state_data(persisted_pending_orders=[None]), "must be an object"),
        (
            valid_state_data(persisted_pending_orders=[{"asset": "SPY"}]),
            "fields are invalid",
        ),
        (
            valid_state_data(
                persisted_pending_orders=[
                    {"asset": "", "side": "buy", "quantity": 1, "order_type": "market"}
                ]
            ),
            "asset is invalid",
        ),
        (
            valid_state_data(
                persisted_pending_orders=[
                    {"asset": "SPY", "side": "up", "quantity": 1, "order_type": "market"}
                ]
            ),
            "enum value is invalid",
        ),
        (
            valid_state_data(
                persisted_pending_orders=[
                    {"asset": "SPY", "side": "buy", "quantity": 0, "order_type": "market"}
                ]
            ),
            "quantity must be positive",
        ),
        (
            valid_state_data(
                persisted_pending_orders=[
                    {
                        "asset": "SPY",
                        "side": "buy",
                        "quantity": 1,
                        "order_type": "limit",
                        "limit_price": -1,
                    }
                ]
            ),
            "limit_price must be positive",
        ),
        (valid_state_data(portable_strategy_state=[]), "must be an object"),
        (valid_state_data(replacement_gaps={1: "bad"}), "non-string key"),
        (valid_state_data(replacement_gaps={"gap": float("nan")}), "non-finite"),
        (valid_state_data(replacement_gaps={"gap": object()}), "unsupported value"),
        (valid_state_data(shadow_portfolio={"positions": {}}), "positions must be a list"),
        (
            valid_state_data(shadow_portfolio={"positions": [None]}),
            "positions must contain objects",
        ),
        (
            valid_state_data(shadow_portfolio={"positions": [{"asset": "", "quantity": 1}]}),
            "position asset is invalid",
        ),
    ],
)
def test_risk_state_rejects_every_corrupt_public_field(payload: Any, message: str) -> None:
    with pytest.raises(CorruptStateError, match=message):
        RiskState.from_dict(payload)


def test_risk_state_save_atomic():
    """Test atomic file write."""
    state = RiskState(
        date="2023-10-15", daily_loss=1500.0, orders_placed=25, high_water_mark=105000.0
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = str(Path(tmpdir) / "test_state.json")

        # Save
        RiskState.save_atomic(state, filepath)

        # Verify file exists
        assert Path(filepath).exists()

        # Verify .tmp file was cleaned up
        assert not Path(f"{filepath}.tmp").exists()

        # Load and verify
        loaded = RiskState.load(filepath)
        assert loaded is not None
        assert loaded.date == "2023-10-15"
        assert loaded.daily_loss == 1500.0
        assert loaded.orders_placed == 25


def test_risk_state_load_nonexistent():
    """Test loading from nonexistent file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = str(Path(tmpdir) / "nonexistent.json")
        state = RiskState.load(filepath)
        assert state is None


def test_risk_state_load_corrupted():
    """Test loading from corrupted JSON file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = str(Path(tmpdir) / "corrupted.json")

        # Write invalid JSON
        with open(filepath, "w") as f:
            f.write("{ this is not valid json }")

        Path(filepath).chmod(0o600)
        with pytest.raises(CorruptStateError):
            RiskState.load(filepath)


def test_risk_state_load_invalid_format():
    """Test loading from file with invalid format."""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = str(Path(tmpdir) / "invalid.json")

        # Write valid JSON but missing required fields
        with open(filepath, "w") as f:
            f.write('{"wrong": "format"}')

        Path(filepath).chmod(0o600)
        with pytest.raises(CorruptStateError):
            RiskState.load(filepath)


def test_risk_state_create_for_today():
    """Test creating state for today's date."""
    state = RiskState.create_for_today()

    # Should have today's date
    today = datetime.now().strftime("%Y-%m-%d")
    assert state.date == today

    # Should have default values
    assert state.daily_loss == 0.0
    assert state.orders_placed == 0
    assert state.high_water_mark == 0.0
    assert state.kill_switch_activated is False
    assert state.kill_switch_reason == ""


def test_risk_state_atomic_overwrite():
    """Test that atomic save correctly overwrites existing file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = str(Path(tmpdir) / "state.json")

        # Save first state
        state1 = RiskState(date="2023-10-15", daily_loss=1000.0)
        RiskState.save_atomic(state1, filepath)

        # Save second state (overwrite)
        state2 = RiskState(date="2023-10-16", daily_loss=2000.0)
        RiskState.save_atomic(state2, filepath)

        # Load and verify second state
        loaded = RiskState.load(filepath)
        assert loaded is not None
        assert loaded.date == "2023-10-16"
        assert loaded.daily_loss == 2000.0
