"""Unit tests for LiveRiskConfig and RiskState.

Tests cover:
- Configuration validation (invalid values raise ValueError)
- Default values
- RiskState serialization (to_dict/from_dict)
- Atomic file operations (save_atomic)
"""

import pytest
import tempfile
from pathlib import Path
from datetime import datetime

from ml4t.live.safety import LiveRiskConfig, RiskState


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
        LiveRiskConfig(
            allowed_assets={"AAPL", "TSLA"}, blocked_assets={"AAPL", "MSFT"}
        )

    # No overlap is OK
    config = LiveRiskConfig(
        allowed_assets={"AAPL", "TSLA"}, blocked_assets={"MSFT", "GOOGL"}
    )
    assert config.allowed_assets == {"AAPL", "TSLA"}
    assert config.blocked_assets == {"MSFT", "GOOGL"}


def test_live_risk_config_validation_state_file():
    """Test validation of state file parameter."""
    # Empty state_file
    with pytest.raises(ValueError, match="state_file cannot be empty"):
        LiveRiskConfig(state_file="")


def test_live_risk_config_inf_values_allowed():
    """Test that infinity values are allowed (to disable limits)."""
    config = LiveRiskConfig(
        max_position_value=float("inf"),
        max_total_exposure=float("inf"),
        max_order_value=float("inf"),
    )

    assert config.max_position_value == float("inf")
    assert config.max_total_exposure == float("inf")
    assert config.max_order_value == float("inf")


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

        # Should return None (not crash)
        state = RiskState.load(filepath)
        assert state is None


def test_risk_state_load_invalid_format():
    """Test loading from file with invalid format."""
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = str(Path(tmpdir) / "invalid.json")

        # Write valid JSON but missing required fields
        with open(filepath, "w") as f:
            f.write('{"wrong": "format"}')

        # Should return None (not crash)
        state = RiskState.load(filepath)
        assert state is None


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
