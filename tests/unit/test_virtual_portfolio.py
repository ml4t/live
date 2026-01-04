"""Unit tests for VirtualPortfolio.

Tests cover all position transitions:
- New position (buy/sell)
- Position increase (weighted average cost basis)
- Position decrease (partial close)
- Position close (full close)
- Position flip (long -> short or short -> long)
- Price updates
- Account value calculation
"""


from ml4t.live.safety import VirtualPortfolio
from ml4t.backtest.types import Order, OrderSide, OrderStatus


def create_order(
    asset: str,
    side: OrderSide,
    quantity: float,
    filled_price: float,
    filled_quantity: float | None = None,
) -> Order:
    """Helper to create filled Order objects for testing.

    Args:
        asset: Asset symbol
        side: BUY or SELL
        quantity: Order quantity
        filled_price: Fill price
        filled_quantity: Filled quantity (defaults to quantity if None)

    Returns:
        Order object with fill information
    """
    if filled_quantity is None:
        filled_quantity = quantity

    return Order(
        asset=asset,
        side=side,
        quantity=quantity,
        order_id=f"test-{asset}-{side.value}",
        status=OrderStatus.FILLED,
        filled_price=filled_price,
        filled_quantity=filled_quantity,
    )


# === Initialization Tests ===


def test_virtual_portfolio_initialization():
    """Test VirtualPortfolio initialization with default values."""
    portfolio = VirtualPortfolio()

    assert portfolio.cash == 100_000.0
    assert len(portfolio.positions) == 0
    assert portfolio.account_value == 100_000.0


def test_virtual_portfolio_custom_cash():
    """Test VirtualPortfolio initialization with custom cash."""
    portfolio = VirtualPortfolio(initial_cash=50_000.0)

    assert portfolio.cash == 50_000.0
    assert portfolio.account_value == 50_000.0


# === New Position Tests ===


def test_open_long_position():
    """Test opening a new long position."""
    portfolio = VirtualPortfolio(initial_cash=100_000.0)

    order = create_order("AAPL", OrderSide.BUY, 100, 150.0)
    portfolio.process_fill(order)

    # Check position
    assert "AAPL" in portfolio.positions
    pos = portfolio.positions["AAPL"]
    assert pos.asset == "AAPL"
    assert pos.quantity == 100.0
    assert pos.entry_price == 150.0
    assert pos.current_price == 150.0

    # Check cash
    assert portfolio.cash == 100_000.0 - (100 * 150.0)
    assert portfolio.cash == 85_000.0

    # Check account value
    assert portfolio.account_value == 100_000.0  # No change (price same)


def test_open_short_position():
    """Test opening a new short position."""
    portfolio = VirtualPortfolio(initial_cash=100_000.0)

    order = create_order("TSLA", OrderSide.SELL, 50, 200.0)
    portfolio.process_fill(order)

    # Check position
    assert "TSLA" in portfolio.positions
    pos = portfolio.positions["TSLA"]
    assert pos.asset == "TSLA"
    assert pos.quantity == -50.0
    assert pos.entry_price == 200.0

    # Check cash (selling adds cash)
    assert portfolio.cash == 100_000.0 + (50 * 200.0)
    assert portfolio.cash == 110_000.0


# === Position Increase Tests ===


def test_increase_long_position():
    """Test increasing a long position with weighted average cost basis."""
    portfolio = VirtualPortfolio(initial_cash=100_000.0)

    # Buy 100 @ 150
    order1 = create_order("AAPL", OrderSide.BUY, 100, 150.0)
    portfolio.process_fill(order1)

    # Buy 50 more @ 160
    order2 = create_order("AAPL", OrderSide.BUY, 50, 160.0)
    portfolio.process_fill(order2)

    # Check position
    pos = portfolio.positions["AAPL"]
    assert pos.quantity == 150.0

    # Weighted average: (100*150 + 50*160) / 150 = (15000 + 8000) / 150 = 153.33
    expected_basis = (100 * 150.0 + 50 * 160.0) / 150.0
    assert abs(pos.entry_price - expected_basis) < 0.01

    # Check cash
    expected_cash = 100_000.0 - (100 * 150.0) - (50 * 160.0)
    assert portfolio.cash == expected_cash


def test_increase_short_position():
    """Test increasing a short position with weighted average cost basis."""
    portfolio = VirtualPortfolio(initial_cash=100_000.0)

    # Short 100 @ 200
    order1 = create_order("TSLA", OrderSide.SELL, 100, 200.0)
    portfolio.process_fill(order1)

    # Short 50 more @ 190
    order2 = create_order("TSLA", OrderSide.SELL, 50, 190.0)
    portfolio.process_fill(order2)

    # Check position
    pos = portfolio.positions["TSLA"]
    assert pos.quantity == -150.0

    # Weighted average: (100*200 + 50*190) / 150 = (20000 + 9500) / 150 = 196.67
    expected_basis = abs((100 * 200.0 + 50 * 190.0) / -150.0)
    assert abs(pos.entry_price - expected_basis) < 0.01


# === Position Decrease Tests ===


def test_partial_close_long():
    """Test partially closing a long position (basis unchanged)."""
    portfolio = VirtualPortfolio(initial_cash=100_000.0)

    # Buy 100 @ 150
    buy_order = create_order("AAPL", OrderSide.BUY, 100, 150.0)
    portfolio.process_fill(buy_order)

    initial_basis = portfolio.positions["AAPL"].entry_price

    # Sell 40 @ 155 (partial close)
    sell_order = create_order("AAPL", OrderSide.SELL, 40, 155.0)
    portfolio.process_fill(sell_order)

    # Check position
    pos = portfolio.positions["AAPL"]
    assert pos.quantity == 60.0
    assert pos.entry_price == initial_basis  # Basis unchanged on decrease
    assert pos.current_price == 155.0

    # Check cash
    expected_cash = 100_000.0 - (100 * 150.0) + (40 * 155.0)
    assert portfolio.cash == expected_cash


def test_partial_close_short():
    """Test partially closing a short position (basis unchanged)."""
    portfolio = VirtualPortfolio(initial_cash=100_000.0)

    # Short 100 @ 200
    sell_order = create_order("TSLA", OrderSide.SELL, 100, 200.0)
    portfolio.process_fill(sell_order)

    initial_basis = portfolio.positions["TSLA"].entry_price

    # Buy back 30 @ 195 (partial close)
    buy_order = create_order("TSLA", OrderSide.BUY, 30, 195.0)
    portfolio.process_fill(buy_order)

    # Check position
    pos = portfolio.positions["TSLA"]
    assert pos.quantity == -70.0
    assert pos.entry_price == initial_basis  # Basis unchanged on decrease


# === Position Close Tests ===


def test_full_close_long():
    """Test fully closing a long position."""
    portfolio = VirtualPortfolio(initial_cash=100_000.0)

    # Buy 100 @ 150
    buy_order = create_order("AAPL", OrderSide.BUY, 100, 150.0)
    portfolio.process_fill(buy_order)

    # Sell 100 @ 155 (full close)
    sell_order = create_order("AAPL", OrderSide.SELL, 100, 155.0)
    portfolio.process_fill(sell_order)

    # Check position removed
    assert "AAPL" not in portfolio.positions

    # Check cash (profit = 5 * 100 = 500)
    expected_cash = 100_000.0 - (100 * 150.0) + (100 * 155.0)
    assert portfolio.cash == expected_cash
    assert portfolio.cash == 100_500.0


def test_full_close_short():
    """Test fully closing a short position."""
    portfolio = VirtualPortfolio(initial_cash=100_000.0)

    # Short 100 @ 200
    sell_order = create_order("TSLA", OrderSide.SELL, 100, 200.0)
    portfolio.process_fill(sell_order)

    # Buy back 100 @ 195 (full close)
    buy_order = create_order("TSLA", OrderSide.BUY, 100, 195.0)
    portfolio.process_fill(buy_order)

    # Check position removed
    assert "TSLA" not in portfolio.positions

    # Check cash (profit = 5 * 100 = 500)
    expected_cash = 100_000.0 + (100 * 200.0) - (100 * 195.0)
    assert portfolio.cash == expected_cash
    assert portfolio.cash == 100_500.0


# === Position Flip Tests ===


def test_flip_long_to_short():
    """Test flipping from long to short position."""
    portfolio = VirtualPortfolio(initial_cash=100_000.0)

    # Buy 100 @ 150 (long)
    buy_order = create_order("AAPL", OrderSide.BUY, 100, 150.0)
    portfolio.process_fill(buy_order)

    # Sell 200 @ 155 (flip to short 100)
    sell_order = create_order("AAPL", OrderSide.SELL, 200, 155.0)
    portfolio.process_fill(sell_order)

    # Check position flipped
    pos = portfolio.positions["AAPL"]
    assert pos.quantity == -100.0
    assert pos.entry_price == 155.0  # Basis reset on flip
    assert pos.current_price == 155.0


def test_flip_short_to_long():
    """Test flipping from short to long position."""
    portfolio = VirtualPortfolio(initial_cash=100_000.0)

    # Short 100 @ 200
    sell_order = create_order("TSLA", OrderSide.SELL, 100, 200.0)
    portfolio.process_fill(sell_order)

    # Buy 200 @ 195 (flip to long 100)
    buy_order = create_order("TSLA", OrderSide.BUY, 200, 195.0)
    portfolio.process_fill(buy_order)

    # Check position flipped
    pos = portfolio.positions["TSLA"]
    assert pos.quantity == 100.0
    assert pos.entry_price == 195.0  # Basis reset on flip


# === Partial Fill Tests ===


def test_partial_fill():
    """Test handling partial fills."""
    portfolio = VirtualPortfolio(initial_cash=100_000.0)

    # Order for 100, but only 60 filled
    order = create_order("AAPL", OrderSide.BUY, 100, 150.0, filled_quantity=60.0)
    portfolio.process_fill(order)

    # Check position reflects partial fill
    pos = portfolio.positions["AAPL"]
    assert pos.quantity == 60.0

    # Check cash reflects partial fill
    expected_cash = 100_000.0 - (60 * 150.0)
    assert portfolio.cash == expected_cash


def test_order_without_fill_info():
    """Test that orders without fill info are ignored."""
    portfolio = VirtualPortfolio(initial_cash=100_000.0)

    # Order without filled_price
    order = Order(
        asset="AAPL",
        side=OrderSide.BUY,
        quantity=100,
        order_id="test",
        status=OrderStatus.PENDING,
    )

    portfolio.process_fill(order)

    # Should be ignored
    assert "AAPL" not in portfolio.positions
    assert portfolio.cash == 100_000.0


# === Price Update Tests ===


def test_update_prices():
    """Test updating current prices for positions."""
    portfolio = VirtualPortfolio(initial_cash=100_000.0)

    # Open positions
    order1 = create_order("AAPL", OrderSide.BUY, 100, 150.0)
    portfolio.process_fill(order1)

    order2 = create_order("TSLA", OrderSide.BUY, 50, 200.0)
    portfolio.process_fill(order2)

    # Update prices
    portfolio.update_prices({"AAPL": 160.0, "TSLA": 210.0, "MSFT": 300.0})

    # Check prices updated
    assert portfolio.positions["AAPL"].current_price == 160.0
    assert portfolio.positions["TSLA"].current_price == 210.0

    # MSFT not in portfolio, should be ignored
    assert "MSFT" not in portfolio.positions


# === Account Value Tests ===


def test_account_value_with_positions():
    """Test account value calculation with positions."""
    portfolio = VirtualPortfolio(initial_cash=100_000.0)

    # Buy AAPL: 100 @ 150
    order1 = create_order("AAPL", OrderSide.BUY, 100, 150.0)
    portfolio.process_fill(order1)

    # Buy TSLA: 50 @ 200
    order2 = create_order("TSLA", OrderSide.BUY, 50, 200.0)
    portfolio.process_fill(order2)

    # Cash: 100000 - 15000 - 10000 = 75000
    # Position value: 15000 + 10000 = 25000
    # Total: 100000
    assert portfolio.account_value == 100_000.0

    # Update prices
    portfolio.update_prices({"AAPL": 160.0, "TSLA": 210.0})

    # New position value: 16000 + 10500 = 26500
    # Total: 75000 + 26500 = 101500
    expected_value = 75_000.0 + (100 * 160.0) + (50 * 210.0)
    assert portfolio.account_value == expected_value


def test_account_value_with_short_positions():
    """Test account value calculation with short positions."""
    portfolio = VirtualPortfolio(initial_cash=100_000.0)

    # Short AAPL: 100 @ 150
    order = create_order("AAPL", OrderSide.SELL, 100, 150.0)
    portfolio.process_fill(order)

    # Cash: 100000 + 15000 = 115000
    # Position value (use abs for shorts): 100 * 150 = 15000
    # Total: 115000 + 15000 = 130000
    assert portfolio.account_value == 130_000.0


# === Complex Scenario Tests ===


def test_multiple_transactions_single_asset():
    """Test complex scenario with multiple transactions on single asset."""
    portfolio = VirtualPortfolio(initial_cash=100_000.0)

    # Buy 100 @ 150
    portfolio.process_fill(create_order("AAPL", OrderSide.BUY, 100, 150.0))
    assert portfolio.positions["AAPL"].quantity == 100.0

    # Buy 50 more @ 160
    portfolio.process_fill(create_order("AAPL", OrderSide.BUY, 50, 160.0))
    assert portfolio.positions["AAPL"].quantity == 150.0

    # Sell 80 @ 165 (partial close)
    portfolio.process_fill(create_order("AAPL", OrderSide.SELL, 80, 165.0))
    assert portfolio.positions["AAPL"].quantity == 70.0

    # Sell 150 @ 170 (flip to short)
    portfolio.process_fill(create_order("AAPL", OrderSide.SELL, 150, 170.0))
    assert portfolio.positions["AAPL"].quantity == -80.0
    assert portfolio.positions["AAPL"].entry_price == 170.0

    # Buy back 80 @ 175 (close)
    portfolio.process_fill(create_order("AAPL", OrderSide.BUY, 80, 175.0))
    assert "AAPL" not in portfolio.positions


def test_multiple_assets():
    """Test managing multiple assets simultaneously."""
    portfolio = VirtualPortfolio(initial_cash=100_000.0)

    # Open multiple positions
    portfolio.process_fill(create_order("AAPL", OrderSide.BUY, 100, 150.0))
    portfolio.process_fill(create_order("TSLA", OrderSide.SELL, 50, 200.0))
    portfolio.process_fill(create_order("MSFT", OrderSide.BUY, 75, 300.0))

    assert len(portfolio.positions) == 3
    assert portfolio.positions["AAPL"].quantity == 100.0
    assert portfolio.positions["TSLA"].quantity == -50.0
    assert portfolio.positions["MSFT"].quantity == 75.0

    # Close one position
    portfolio.process_fill(create_order("TSLA", OrderSide.BUY, 50, 195.0))
    assert len(portfolio.positions) == 2
    assert "TSLA" not in portfolio.positions


# === Edge Cases ===


def test_positions_property_returns_copy():
    """Test that positions property returns a copy (not mutable reference)."""
    portfolio = VirtualPortfolio(initial_cash=100_000.0)

    portfolio.process_fill(create_order("AAPL", OrderSide.BUY, 100, 150.0))

    positions_copy = portfolio.positions

    # Modifying the copy shouldn't affect the portfolio
    positions_copy.pop("AAPL")

    # Original should still have the position
    assert "AAPL" in portfolio.positions
