"""Unit tests for SafeBroker.

Tests cover:
- Initialization and state loading
- Shadow mode routing to VirtualPortfolio
- Risk check methods
- Kill switch functionality
- State persistence
- Memory leak prevention
"""

import json
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from ml4t.backtest.types import Order, OrderSide, OrderStatus, OrderType, Position
from ml4t.specs import ExecutionCapability

from ml4t.live.safety import (
    LiveRiskConfig,
    ReconciliationMismatchError,
    RiskLimitError,
    RiskState,
    SafeBroker,
)

# === Fixtures ===


@pytest.fixture
def mock_broker():
    """Create a mock async broker."""
    broker = MagicMock()
    broker._connected = True
    broker.execution_capabilities = frozenset(
        {
            ExecutionCapability.LIMIT,
            ExecutionCapability.STOP,
            ExecutionCapability.STOP_LIMIT,
            ExecutionCapability.CLOSE_AUCTION,
        }
    )
    broker.positions = {}
    broker.pending_orders = []
    broker.get_position = MagicMock(return_value=None)
    broker.get_positions_async = AsyncMock(return_value={})
    broker.get_pending_orders_async = AsyncMock(return_value=[])
    broker.get_account_value_async = AsyncMock(return_value=100_000.0)
    broker.get_cash_async = AsyncMock(return_value=50_000.0)
    broker.submit_order_async = AsyncMock()
    broker.cancel_order_async = AsyncMock(return_value=True)
    broker.replace_order_async = AsyncMock()
    broker.close_position_async = AsyncMock()
    broker.connect = AsyncMock()
    broker.disconnect = AsyncMock()
    broker.is_connected_async = AsyncMock(return_value=True)
    broker.assert_paper_trading = MagicMock()
    broker.assert_live_trading = MagicMock()
    return broker


@pytest.fixture
def temp_state_file():
    """Create a temporary state file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        path = f.name
    Path(path).unlink()
    yield path
    Path(path).unlink(missing_ok=True)
    Path(f"{path}.lock").unlink(missing_ok=True)


def write_legacy_state(path: str, state: RiskState) -> None:
    target = Path(path)
    target.write_text(json.dumps(state.to_dict(), indent=2))
    target.chmod(0o600)


@pytest.fixture
def config(temp_state_file):
    """Create a risk config with temp state file."""
    return LiveRiskConfig(
        max_position_value=10_000.0,
        max_order_value=5_000.0,
        max_orders_per_minute=5,
        max_daily_loss=1_000.0,
        max_drawdown_pct=0.10,
        shadow_mode=False,
        execution_mode="paper",
        state_file=temp_state_file,
    )


# === Initialization Tests ===


def test_safe_broker_init(mock_broker, config):
    """Test SafeBroker initialization."""
    safe = SafeBroker(mock_broker, config)

    assert safe._broker is mock_broker
    assert safe.config == config
    assert safe._state is not None
    assert isinstance(safe._virtual_portfolio.positions, dict)
    assert len(safe._order_timestamps) == 0
    assert len(safe._recent_orders) == 0


def test_safe_broker_loads_existing_state(mock_broker, temp_state_file):
    """Test SafeBroker loads existing state from file."""
    # Use today's date so daily counters aren't reset
    today = datetime.now().strftime("%Y-%m-%d")
    state = RiskState(
        date=today,
        daily_loss=500.0,
        orders_placed=10,
        high_water_mark=105_000.0,
    )
    write_legacy_state(temp_state_file, state)

    config = LiveRiskConfig(execution_mode="paper", state_file=temp_state_file)
    safe = SafeBroker(mock_broker, config)

    assert safe._state.daily_loss == 500.0
    assert safe._state.orders_placed == 10
    assert safe._state.high_water_mark == 105_000.0


# === Shadow Mode Tests ===


def test_positions_property_shadow_mode(mock_broker, config):
    """Test positions property returns virtual positions in shadow mode."""
    config.shadow_mode = True
    config.execution_mode = "shadow"
    safe = SafeBroker(mock_broker, config)

    # Virtual portfolio starts empty
    assert safe.positions == {}

    # After virtual fill, should return virtual positions
    safe._virtual_portfolio._positions["AAPL"] = Position(
        asset="AAPL", quantity=100, entry_price=150.0, entry_time=datetime.now()
    )

    assert "AAPL" in safe.positions
    assert safe.positions["AAPL"].quantity == 100


def test_positions_property_live_mode(mock_broker, config):
    """Test positions property returns broker positions in live mode."""
    config.shadow_mode = False
    mock_broker.positions = {
        "SPY": Position(asset="SPY", quantity=50, entry_price=400.0, entry_time=datetime.now())
    }

    safe = SafeBroker(mock_broker, config)

    assert safe.positions == mock_broker.positions


def test_get_position_shadow_mode(mock_broker, config):
    """Test get_position returns virtual position in shadow mode."""
    config.shadow_mode = True
    config.execution_mode = "shadow"
    safe = SafeBroker(mock_broker, config)

    safe._virtual_portfolio._positions["AAPL"] = Position(
        asset="AAPL", quantity=100, entry_price=150.0, entry_time=datetime.now()
    )

    pos = safe.get_position("AAPL")
    assert pos is not None
    assert pos.quantity == 100


async def test_get_account_value_shadow_mode(mock_broker, config):
    """Test get_account_value_async returns virtual value in shadow mode."""
    config.shadow_mode = True
    config.execution_mode = "shadow"
    safe = SafeBroker(mock_broker, config)

    value = await safe.get_account_value_async()
    assert value == safe._virtual_portfolio.account_value


async def test_get_cash_shadow_mode(mock_broker, config):
    """Test get_cash_async returns virtual cash in shadow mode."""
    config.shadow_mode = True
    config.execution_mode = "shadow"
    safe = SafeBroker(mock_broker, config)

    cash = await safe.get_cash_async()
    assert cash == safe._virtual_portfolio.cash


# === Risk Check Tests ===


def test_check_asset_blocked(mock_broker, config):
    """Test _check_asset raises error for blocked assets."""
    config.blocked_assets = {"GME", "AMC"}
    safe = SafeBroker(mock_broker, config)

    with pytest.raises(RiskLimitError, match="GME is blocked"):
        safe._check_asset("GME")


def test_check_asset_not_allowed(mock_broker, config):
    """Test _check_asset raises error for assets not in allowed list."""
    config.allowed_assets = {"AAPL", "MSFT"}
    safe = SafeBroker(mock_broker, config)

    with pytest.raises(RiskLimitError, match="not in allowed list"):
        safe._check_asset("TSLA")


def test_check_asset_passes(mock_broker, config):
    """Test _check_asset passes for allowed assets."""
    config.allowed_assets = {"AAPL"}
    safe = SafeBroker(mock_broker, config)

    # Should not raise
    safe._check_asset("AAPL")


def test_check_duplicate_detects_duplicate(mock_broker, config):
    """Test _check_duplicate detects duplicate orders."""
    safe = SafeBroker(mock_broker, config)

    # Submit first order
    safe._recent_orders.append((time.time(), "AAPL", 100.0))

    # Immediate duplicate should be caught
    with pytest.raises(RiskLimitError, match="Duplicate order blocked"):
        safe._check_duplicate("AAPL", 100.0)


def test_check_duplicate_allows_different_asset(mock_broker, config):
    """Test _check_duplicate allows different assets."""
    safe = SafeBroker(mock_broker, config)

    safe._recent_orders.append((time.time(), "AAPL", 100.0))

    # Different asset should pass
    safe._check_duplicate("MSFT", 100.0)


def test_check_duplicate_allows_different_quantity(mock_broker, config):
    """Test _check_duplicate allows different quantities."""
    safe = SafeBroker(mock_broker, config)

    safe._recent_orders.append((time.time(), "AAPL", 100.0))

    # Different quantity should pass
    safe._check_duplicate("AAPL", 200.0)


def test_check_duplicate_ignores_old_entries_without_mutating_history(mock_broker, config):
    """Test rejected checks do not mutate accepted-order history."""
    safe = SafeBroker(mock_broker, config)

    # Add old order (beyond dedup window)
    safe._recent_orders.append((time.time() - 5.0, "AAPL", 100.0))

    # Should pass since old entry is pruned
    safe._check_duplicate("AAPL", 100.0)

    assert len(safe._recent_orders) == 1


def test_check_rate_limit_enforced(mock_broker, config):
    """Test _check_rate_limit enforces max orders per minute."""
    config.max_orders_per_minute = 3
    safe = SafeBroker(mock_broker, config)

    safe._order_timestamps = [time.time()] * 3

    # 4th order should fail
    with pytest.raises(RiskLimitError, match="Rate limit"):
        safe._check_rate_limit()


def test_check_rate_limit_prunes_old_timestamps(mock_broker, config):
    """Test _check_rate_limit prunes timestamps older than 1 minute."""
    config.max_orders_per_minute = 2
    safe = SafeBroker(mock_broker, config)

    # Add old timestamps (>60s ago)
    safe._order_timestamps = [time.time() - 65.0, time.time() - 70.0]

    # Should pass since old timestamps are pruned
    safe._check_rate_limit()
    safe._check_rate_limit()


def test_check_order_limits_quantity(mock_broker, config):
    """Test _check_order_limits enforces max order quantity."""
    config.max_order_shares = 100
    safe = SafeBroker(mock_broker, config)

    with pytest.raises(RiskLimitError, match="quantity.*exceeds max"):
        safe._check_order_limits(quantity=150, value=1000.0)


def test_check_order_limits_value(mock_broker, config):
    """Test _check_order_limits enforces max order value."""
    config.max_order_value = 5_000.0
    safe = SafeBroker(mock_broker, config)

    with pytest.raises(RiskLimitError, match="value.*exceeds max"):
        safe._check_order_limits(quantity=100, value=6_000.0)


async def test_check_position_limits_max_position_value(mock_broker, config):
    """Test _check_position_limits enforces max position value."""
    config.max_position_value = 10_000.0
    safe = SafeBroker(mock_broker, config)

    # Mock existing position worth $8,000
    safe._broker.positions = {
        "AAPL": Position(
            asset="AAPL",
            quantity=50,
            entry_price=160.0,
            entry_time=datetime.now(),
            current_price=160.0,
        )
    }
    mock_broker.get_position = lambda a: mock_broker.positions.get(a)

    # Try to add $5,000 more (would exceed $10k limit)
    with pytest.raises(RiskLimitError, match="Position value.*would exceed"):
        await safe._check_position_limits(
            asset="AAPL", quantity=30, order_value=5_000.0, side=OrderSide.BUY
        )


async def test_check_position_limits_max_position_shares(mock_broker, config):
    """Test _check_position_limits enforces max position shares."""
    config.max_position_shares = 100
    config.max_position_value = 100_000.0  # Set high to avoid value check
    safe = SafeBroker(mock_broker, config)

    # Mock existing position of 80 shares
    safe._broker.positions = {
        "AAPL": Position(asset="AAPL", quantity=80, entry_price=150.0, entry_time=datetime.now())
    }
    mock_broker.get_position = lambda a: mock_broker.positions.get(a)

    # Try to add 30 more shares (would exceed 100 limit)
    with pytest.raises(RiskLimitError, match="Position quantity.*would exceed"):
        await safe._check_position_limits(
            asset="AAPL", quantity=30, order_value=4_500.0, side=OrderSide.BUY
        )


async def test_check_position_limits_max_total_exposure(mock_broker, config):
    """Test _check_position_limits enforces max total exposure."""
    config.max_total_exposure = 15_000.0
    safe = SafeBroker(mock_broker, config)

    # Mock positions totaling $12,000
    safe._broker.positions = {
        "AAPL": Position(
            asset="AAPL",
            quantity=50,
            entry_price=150.0,
            entry_time=datetime.now(),
            current_price=150.0,
        ),
        "MSFT": Position(
            asset="MSFT",
            quantity=20,
            entry_price=300.0,
            entry_time=datetime.now(),
            current_price=300.0,
        ),
    }
    mock_broker.get_position = lambda a: mock_broker.positions.get(a)

    # Try to add $5,000 more (would exceed $15k total)
    with pytest.raises(RiskLimitError, match="Total exposure.*would exceed"):
        await safe._check_position_limits(
            asset="TSLA", quantity=20, order_value=5_000.0, side=OrderSide.BUY
        )


async def test_check_position_limits_allows_reducing_exposure(mock_broker, config):
    """Test _check_position_limits allows trades that reduce risk."""
    config.max_position_value = 10_000.0
    config.max_total_exposure = 10_000.0
    safe = SafeBroker(mock_broker, config)

    safe._broker.positions = {
        "AAPL": Position(
            asset="AAPL",
            quantity=100,
            entry_price=150.0,
            entry_time=datetime.now(),
            current_price=150.0,
        )
    }
    mock_broker.get_position = lambda a: mock_broker.positions.get(a)

    await safe._check_position_limits(
        asset="AAPL", quantity=40, order_value=6_000.0, side=OrderSide.SELL
    )


async def test_check_position_limits_max_positions(mock_broker, config):
    """Test _check_position_limits enforces max number of positions."""
    config.max_positions = 2
    safe = SafeBroker(mock_broker, config)

    # Mock 2 existing positions (at limit)
    safe._broker.positions = {
        "AAPL": Position(asset="AAPL", quantity=10, entry_price=150.0, entry_time=datetime.now()),
        "MSFT": Position(asset="MSFT", quantity=10, entry_price=300.0, entry_time=datetime.now()),
    }
    mock_broker.get_position = lambda a: mock_broker.positions.get(a)

    # Try to open 3rd position
    with pytest.raises(RiskLimitError, match="Max positions.*reached"):
        await safe._check_position_limits(
            asset="TSLA", quantity=10, order_value=2_500.0, side=OrderSide.BUY
        )


async def test_check_price_deviation_rejects_far_limit(mock_broker, config):
    """Test _check_price_deviation rejects limit orders far from market."""
    config.max_price_deviation_pct = 0.05  # 5%
    safe = SafeBroker(mock_broker, config)

    # Mock position with current price $100
    safe._broker.positions = {
        "AAPL": Position(
            asset="AAPL",
            quantity=10,
            entry_price=100.0,
            entry_time=datetime.now(),
            current_price=100.0,
        )
    }
    mock_broker.get_position = lambda a: mock_broker.positions.get(a)

    # Try limit order at $120 (20% deviation)
    with pytest.raises(RiskLimitError, match="Price deviation"):
        await safe._check_price_deviation(asset="AAPL", limit_price=120.0)


async def test_check_price_deviation_allows_close_limit(mock_broker, config):
    """Test _check_price_deviation allows limit orders close to market."""
    config.max_price_deviation_pct = 0.05  # 5%
    safe = SafeBroker(mock_broker, config)

    # Mock position with current price $100
    safe._broker.positions = {
        "AAPL": Position(
            asset="AAPL",
            quantity=10,
            entry_price=100.0,
            entry_time=datetime.now(),
            current_price=100.0,
        )
    }
    mock_broker.get_position = lambda a: mock_broker.positions.get(a)

    # Try limit order at $103 (3% deviation - within 5% limit)
    await safe._check_price_deviation(asset="AAPL", limit_price=103.0)


async def test_check_drawdown_activates_kill_switch(mock_broker, config):
    """Test _check_drawdown activates kill switch on excessive drawdown."""
    config.max_drawdown_pct = 0.10  # 10%
    safe = SafeBroker(mock_broker, config)

    # Set high water mark
    safe._state.high_water_mark = 100_000.0

    # Mock current value at $85k (15% drawdown)
    mock_broker.get_account_value_async = AsyncMock(return_value=85_000.0)

    with pytest.raises(RiskLimitError, match="Drawdown"):
        await safe._check_drawdown()

    # Kill switch should be activated
    assert safe._state.kill_switch_activated
    assert "Drawdown" in safe._state.kill_switch_reason


async def test_check_drawdown_updates_high_water_mark(mock_broker, config):
    """Test _check_drawdown updates high water mark when value increases."""
    safe = SafeBroker(mock_broker, config)

    safe._state.high_water_mark = 100_000.0
    mock_broker.get_account_value_async = AsyncMock(return_value=105_000.0)

    await safe._check_drawdown()

    assert safe._state.high_water_mark == 105_000.0


async def test_estimate_price_uses_limit_price(mock_broker, config):
    """Test _estimate_price uses limit price if provided."""
    safe = SafeBroker(mock_broker, config)

    price = await safe._estimate_price(asset="AAPL", limit_price=150.0)
    assert price == 150.0


async def test_estimate_price_uses_current_price(mock_broker, config):
    """Test _estimate_price uses current price from position."""
    safe = SafeBroker(mock_broker, config)

    safe._broker.positions = {
        "AAPL": Position(
            asset="AAPL",
            quantity=10,
            entry_price=140.0,
            entry_time=datetime.now(),
            current_price=155.0,
        )
    }
    mock_broker.get_position = lambda a: mock_broker.positions.get(a)

    price = await safe._estimate_price(asset="AAPL", limit_price=None)
    assert price == 155.0


async def test_estimate_price_uses_latest_market_snapshot(mock_broker, config):
    """Test _estimate_price prefers the latest market snapshot for first entries."""
    safe = SafeBroker(mock_broker, config)
    safe._record_market_data(datetime.now(UTC), {"AAPL": {"close": 175.0}}, {})

    price = await safe._estimate_price(asset="AAPL", limit_price=None)
    assert price == 175.0


async def test_check_price_deviation_uses_market_snapshot_for_new_position(mock_broker, config):
    """Test _check_price_deviation works for first-entry orders with market snapshots."""
    config.max_price_deviation_pct = 0.05
    safe = SafeBroker(mock_broker, config)
    safe._record_market_data(datetime.now(UTC), {"AAPL": {"close": 100.0}}, {})

    with pytest.raises(RiskLimitError, match="Price deviation"):
        await safe._check_price_deviation(asset="AAPL", limit_price=110.0)


def test_check_data_staleness_requires_market_data(mock_broker, config):
    """Test _check_data_staleness rejects orders with no market reference."""
    safe = SafeBroker(mock_broker, config)

    with pytest.raises(RiskLimitError, match="No market data available"):
        safe._check_data_staleness("AAPL")


def test_check_data_staleness_rejects_stale_market_data(mock_broker, config):
    """Test _check_data_staleness rejects stale observed market data."""
    config.max_data_staleness_seconds = 30.0
    safe = SafeBroker(mock_broker, config)
    safe._record_market_data(datetime.now(UTC), {"AAPL": {"close": 150.0}}, {})
    safe._latest_market_data["AAPL"].observed_at -= timedelta(seconds=90)

    with pytest.raises(RiskLimitError, match="Stale market data"):
        safe._check_data_staleness("AAPL")


async def test_check_daily_loss_initializes_session_start_equity(mock_broker, config):
    """Test _check_daily_loss captures the first session equity baseline."""
    safe = SafeBroker(mock_broker, config)
    mock_broker.get_account_value_async = AsyncMock(return_value=100_000.0)

    await safe._check_daily_loss()

    assert safe._state.session_start_equity == 100_000.0
    assert safe._state.daily_loss == 0.0


async def test_check_daily_loss_activates_kill_switch(mock_broker, config):
    """Test _check_daily_loss enforces the configured daily-loss limit."""
    safe = SafeBroker(mock_broker, config)
    safe._state.session_start_equity = 100_000.0
    mock_broker.get_account_value_async = AsyncMock(return_value=98_500.0)

    with pytest.raises(RiskLimitError, match="Daily loss"):
        await safe._check_daily_loss()

    assert safe._state.daily_loss == 1_500.0
    assert safe._state.kill_switch_activated is True


# === Kill Switch Tests ===


def test_activate_kill_switch(mock_broker, config):
    """Test _activate_kill_switch sets state correctly."""
    safe = SafeBroker(mock_broker, config)

    safe._activate_kill_switch("Test reason")

    assert safe._state.kill_switch_activated
    assert safe._state.kill_switch_reason == "Test reason"
    assert safe.config.kill_switch_enabled


def test_enable_kill_switch_manual(mock_broker, config):
    """Test enable_kill_switch manual activation."""
    safe = SafeBroker(mock_broker, config)

    safe.enable_kill_switch("Manual stop")

    assert safe._state.kill_switch_activated
    assert safe._state.kill_switch_reason == "Manual stop"


def test_disable_kill_switch(mock_broker, config):
    """Test disable_kill_switch clears state."""
    safe = SafeBroker(mock_broker, config)

    # Activate first
    safe._activate_kill_switch("Test")

    # Disable
    safe.disable_kill_switch()

    assert not safe._state.kill_switch_activated
    assert safe._state.kill_switch_reason == ""
    assert not safe.config.kill_switch_enabled


async def test_submit_order_blocked_by_kill_switch(mock_broker, config):
    """Test submit_order_async raises error when kill switch active."""
    safe = SafeBroker(mock_broker, config)

    safe._activate_kill_switch("Drawdown exceeded")

    with pytest.raises(RiskLimitError, match="Kill switch active"):
        await safe.submit_order_async(asset="AAPL", quantity=10)


async def test_close_all_positions(mock_broker, config):
    """Test close_all_positions closes all positions."""
    config.shadow_mode = False
    safe = SafeBroker(mock_broker, config)

    # Mock positions
    safe._broker.positions = {
        "AAPL": Position(asset="AAPL", quantity=10, entry_price=150.0, entry_time=datetime.now()),
        "MSFT": Position(asset="MSFT", quantity=5, entry_price=300.0, entry_time=datetime.now()),
    }
    mock_broker.get_position = lambda a: mock_broker.positions.get(a)

    # Mock close_position_async to return orders
    mock_broker.close_position_async = AsyncMock(
        side_effect=[
            Order(
                asset="AAPL",
                side=OrderSide.SELL,
                quantity=10,
                order_type=OrderType.MARKET,
                order_id="1",
                created_at=datetime.now(UTC),
            ),
            Order(
                asset="MSFT",
                side=OrderSide.SELL,
                quantity=5,
                order_type=OrderType.MARKET,
                order_id="2",
                created_at=datetime.now(UTC),
            ),
        ]
    )

    orders = await safe.close_all_positions()

    assert len(orders) == 2
    assert orders[0].asset == "AAPL"
    assert orders[1].asset == "MSFT"


# === State Persistence Tests ===


def test_save_state_atomic_write(mock_broker, config):
    """Test _save_state uses atomic write."""
    safe = SafeBroker(mock_broker, config)

    safe._state.orders_placed = 42
    safe._save_state()

    # File should exist
    assert Path(config.state_file).exists()

    # Content should be valid JSON
    data = json.loads(Path(config.state_file).read_text())["payload"]
    assert data["orders_placed"] == 42


def test_load_state_resets_daily_counters_on_new_day(mock_broker, temp_state_file):
    """Test _load_state resets daily counters on new day."""
    from datetime import date, timedelta

    # Write state from yesterday
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    state = RiskState(
        date=yesterday,
        daily_loss=500.0,
        orders_placed=10,
    )
    write_legacy_state(temp_state_file, state)

    config = LiveRiskConfig(execution_mode="paper", state_file=temp_state_file)
    safe = SafeBroker(mock_broker, config)

    # Daily counters should be reset
    assert safe._state.daily_loss == 0.0
    assert safe._state.orders_placed == 0
    assert safe._state.date == date.today().isoformat()


def test_load_state_preserves_kill_switch_across_days(mock_broker, temp_state_file):
    """Test _load_state preserves kill switch state even on new day."""
    from datetime import date, timedelta

    # Write state from yesterday with kill switch
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    state = RiskState(
        date=yesterday,
        kill_switch_activated=True,
        kill_switch_reason="Max drawdown",
    )
    write_legacy_state(temp_state_file, state)

    config = LiveRiskConfig(execution_mode="paper", state_file=temp_state_file)
    safe = SafeBroker(mock_broker, config)

    # Kill switch should be preserved
    assert safe._state.kill_switch_activated
    assert safe._state.kill_switch_reason == "Max drawdown"


# === Memory Leak Prevention Tests ===


def test_prune_history_removes_old_timestamps(mock_broker, config):
    """Test _prune_history removes old order timestamps."""
    safe = SafeBroker(mock_broker, config)

    # Add old timestamps (>60s ago)
    safe._order_timestamps = [
        time.time() - 70.0,
        time.time() - 30.0,
        time.time(),
    ]

    safe._prune_history()

    # Only recent timestamps should remain
    assert len(safe._order_timestamps) == 2


def test_prune_history_removes_old_recent_orders(mock_broker, config):
    """Test _prune_history removes old recent orders."""
    config.dedup_window_seconds = 1.0
    safe = SafeBroker(mock_broker, config)

    # Add old orders (>1hr ago)
    safe._recent_orders = [
        (time.time() - 3700.0, "AAPL", 100.0),  # Too old
        (time.time() - 30.0, "MSFT", 50.0),  # Recent
        (time.time(), "TSLA", 25.0),  # Recent
    ]

    safe._prune_history()

    # Only recent orders should remain
    assert len(safe._recent_orders) == 2


# === Submit Order Integration Tests ===


async def test_submit_order_shadow_mode(mock_broker, config):
    """Test submit_order_async in shadow mode."""
    config.shadow_mode = True
    config.execution_mode = "shadow"
    safe = SafeBroker(mock_broker, config)

    safe._record_market_data(datetime.now(UTC), {"AAPL": {"close": 150.0}}, {})

    order = await safe.submit_order_async(
        asset="AAPL",
        quantity=10,
        side=OrderSide.BUY,
    )

    # Order should be marked as filled
    assert order.status == OrderStatus.FILLED
    assert order.asset == "AAPL"
    assert order.quantity == 10

    # Virtual portfolio should be updated
    pos = safe._virtual_portfolio.positions.get("AAPL")
    assert pos is not None
    assert pos.quantity == 10

    # Broker should NOT be called
    mock_broker.submit_order_async.assert_not_called()


async def test_submit_order_live_mode(mock_broker, config):
    """Test submit_order_async in live mode."""
    config.shadow_mode = False
    safe = SafeBroker(mock_broker, config)

    # Mock broker response
    expected_order = Order(
        asset="AAPL",
        side=OrderSide.BUY,
        quantity=10,
        order_type=OrderType.MARKET,
        order_id="1",
        status=OrderStatus.PENDING,
        created_at=datetime.now(UTC),
    )
    mock_broker.submit_order_async = AsyncMock(return_value=expected_order)

    safe._record_market_data(datetime.now(UTC), {"AAPL": {"close": 150.0}}, {})

    order = await safe.submit_order_async(
        asset="AAPL",
        quantity=10,
        side=OrderSide.BUY,
    )

    # Broker should be called
    mock_broker.submit_order_async.assert_called_once()
    assert order == expected_order


async def test_submit_order_live_mode_passes_moc_through(mock_broker, config):
    """Test SafeBroker passes MOC orders through unchanged."""
    config.shadow_mode = False
    safe = SafeBroker(mock_broker, config)

    expected_order = Order(
        asset="AAPL",
        side=OrderSide.SELL,
        quantity=10,
        order_type=OrderType.MOC,
        order_id="1",
        status=OrderStatus.PENDING,
        created_at=datetime.now(UTC),
    )
    mock_broker.submit_order_async = AsyncMock(return_value=expected_order)

    safe._record_market_data(datetime.now(UTC), {"AAPL": {"close": 150.0}}, {})

    order = await safe.submit_order_async(
        asset="AAPL",
        quantity=10,
        side=OrderSide.SELL,
        order_type=OrderType.MOC,
    )

    assert order == expected_order
    assert mock_broker.submit_order_async.call_args.args[3] == OrderType.MOC


async def test_submit_order_increments_state(mock_broker, config):
    """Test submit_order_async increments order counter."""
    config.shadow_mode = True
    config.execution_mode = "shadow"
    safe = SafeBroker(mock_broker, config)

    initial_count = safe._state.orders_placed

    safe._record_market_data(datetime.now(UTC), {"AAPL": {"close": 150.0}}, {})

    await safe.submit_order_async(asset="AAPL", quantity=10)

    assert safe._state.orders_placed == initial_count + 1


async def test_submit_order_auto_detects_side(mock_broker, config):
    """Test submit_order_async auto-detects side from quantity sign."""
    config.shadow_mode = True
    config.execution_mode = "shadow"
    safe = SafeBroker(mock_broker, config)

    safe._record_market_data(datetime.now(UTC), {"AAPL": {"close": 150.0}}, {})

    # Negative quantity should become SELL
    order = await safe.submit_order_async(asset="AAPL", quantity=-10)

    assert order.side == OrderSide.SELL
    assert order.quantity == 10  # Absolute value


async def test_submit_order_uses_market_price_for_first_entry_limit_checks(mock_broker, config):
    """Test first-entry risk sizing uses the latest market snapshot rather than a fake fallback."""
    config.shadow_mode = True
    config.execution_mode = "shadow"
    config.max_order_value = 1_200.0
    safe = SafeBroker(mock_broker, config)
    safe._record_market_data(datetime.now(UTC), {"AAPL": {"close": 150.0}}, {})

    with pytest.raises(RiskLimitError, match="Order value"):
        await safe.submit_order_async(asset="AAPL", quantity=10, side=OrderSide.BUY)


# === Connection Passthrough Tests ===


async def test_connect_passthrough(mock_broker, config):
    """Test connect passes through to broker."""
    safe = SafeBroker(mock_broker, config)

    await safe.connect()

    mock_broker.connect.assert_called_once()
    assert safe.reconciliation_report is not None
    assert safe.reconciliation_report["clean"] is True


async def test_connect_reconciles_startup_state(mock_broker, config):
    """Test connect reports mismatches between persisted and live broker state."""
    today = datetime.now().strftime("%Y-%m-%d")
    state = RiskState(
        date=today,
        persisted_positions={"AAPL": 10.0},
        persisted_pending_orders=[
            {
                "asset": "AAPL",
                "side": "buy",
                "quantity": 10.0,
                "order_type": "limit",
                "limit_price": 150.0,
            }
        ],
    )
    write_legacy_state(config.state_file, state)
    mock_broker.get_positions_async = AsyncMock(
        return_value={
            "MSFT": Position(
                asset="MSFT",
                quantity=5,
                entry_price=300.0,
                entry_time=datetime.now(UTC),
            )
        }
    )
    mock_broker.get_pending_orders_async = AsyncMock(return_value=[])

    safe = SafeBroker(mock_broker, config)
    await safe.connect()

    report = safe.reconciliation_report
    assert report is not None
    assert report["clean"] is False
    assert report["missing_positions"] == {"AAPL": 10.0}
    assert report["unexpected_positions"] == {"MSFT": 5.0}
    assert len(report["missing_pending_orders"]) == 1


async def test_connect_blocks_on_mismatch_when_configured(mock_broker, config):
    """Test startup can fail closed when reconciliation mismatches are detected."""
    today = datetime.now().strftime("%Y-%m-%d")
    state = RiskState(date=today, persisted_positions={"AAPL": 10.0})
    write_legacy_state(config.state_file, state)
    config.fail_on_reconciliation_mismatch = True
    mock_broker.get_positions_async = AsyncMock(return_value={})

    safe = SafeBroker(mock_broker, config)

    with pytest.raises(ReconciliationMismatchError):
        await safe.connect()

    mock_broker.disconnect.assert_called_once()


async def test_preflight_async_returns_report_without_persisting_state(mock_broker, config):
    """Test preflight builds a report without mutating the saved state snapshot."""
    safe = SafeBroker(mock_broker, config)
    result = await safe.preflight_async()

    assert result["passed"] is True
    assert result["reconciliation"]["clean"] is True
    assert result["journal_file"].endswith(".jsonl")
    assert not Path(config.state_file).exists()


async def test_disconnect_saves_state(mock_broker, config):
    """Test disconnect saves state before disconnecting."""
    safe = SafeBroker(mock_broker, config)

    safe._state.orders_placed = 42
    mock_broker.get_positions_async = AsyncMock(
        return_value={
            "AAPL": Position(
                asset="AAPL",
                quantity=3,
                entry_price=150.0,
                entry_time=datetime.now(UTC),
            )
        }
    )
    mock_broker.get_pending_orders_async = AsyncMock(
        return_value=[
            Order(
                asset="AAPL",
                side=OrderSide.BUY,
                quantity=3,
                order_type=OrderType.LIMIT,
                limit_price=149.5,
                order_id="ML4T-1",
                created_at=datetime.now(UTC),
            )
        ]
    )
    await safe.disconnect()

    data = json.loads(Path(config.state_file).read_text())["payload"]
    assert data["orders_placed"] == 42
    assert data["persisted_positions"] == {"AAPL": 3.0}
    assert data["persisted_pending_orders"] == [
        {
            "asset": "AAPL",
            "side": "buy",
            "quantity": 3.0,
            "order_type": "limit",
            "limit_price": 149.5,
        }
    ]

    mock_broker.disconnect.assert_called_once()


async def test_replace_order_async_cancels_and_resubmits(mock_broker, config):
    """Test normalized replace_order_async uses cancel then submit with updated fields."""
    original = Order(
        asset="AAPL",
        side=OrderSide.BUY,
        quantity=10,
        order_type=OrderType.LIMIT,
        limit_price=150.0,
        order_id="ML4T-1",
        status=OrderStatus.PENDING,
        created_at=datetime.now(UTC),
    )
    replacement = Order(
        asset="AAPL",
        side=OrderSide.BUY,
        quantity=10,
        order_type=OrderType.LIMIT,
        limit_price=149.5,
        order_id="ML4T-2",
        status=OrderStatus.PENDING,
        created_at=datetime.now(UTC),
    )
    mock_broker.pending_orders = [original]
    mock_broker.submit_order_async = AsyncMock(return_value=replacement)

    safe = SafeBroker(mock_broker, config)
    safe._record_market_data(datetime.now(UTC), {"AAPL": {"close": 149.5}}, {})

    result = await safe.replace_order_async("ML4T-1", limit_price=149.5)

    assert result.order_id == "ML4T-2"
    mock_broker.cancel_order_async.assert_called_once_with("ML4T-1")
    mock_broker.submit_order_async.assert_called_once()


async def test_journal_records_runtime_events(mock_broker, config, tmp_path: Path):
    """Test journal captures structured order and kill-switch events."""
    config.shadow_mode = True
    config.execution_mode = "shadow"
    config.journal_file = str(tmp_path / "runtime.jsonl")
    safe = SafeBroker(mock_broker, config)
    safe._record_market_data(datetime.now(UTC), {"AAPL": {"close": 150.0}}, {})

    await safe.submit_order_async(asset="AAPL", quantity=5, side=OrderSide.BUY)
    safe.enable_kill_switch("manual test")

    entries = [json.loads(line) for line in Path(config.journal_file).read_text().splitlines()]
    events = [entry["event"] for entry in entries]

    assert "shadow_order_filled" in events
    assert "kill_switch_activated" in events


async def test_is_connected_async_passthrough(mock_broker, config):
    """Test is_connected_async passes through to broker."""
    safe = SafeBroker(mock_broker, config)

    result = await safe.is_connected_async()

    assert result is True
    mock_broker.is_connected_async.assert_called_once()
