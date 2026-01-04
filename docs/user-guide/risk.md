# Risk Controls

Comprehensive risk management for live trading.

## LiveRiskConfig

The `LiveRiskConfig` class provides all risk parameters:

```python
from ml4t.live import LiveRiskConfig

config = LiveRiskConfig(
    # Mode
    shadow_mode=True,  # No real orders

    # Position Limits
    max_position_value=25_000,
    max_shares=1000,
    max_positions=10,

    # Order Limits
    max_order_value=10_000,

    # Loss Limits
    max_daily_loss=2_000,
    max_drawdown_pct=0.10,

    # Safety Checks
    max_price_deviation_pct=0.05,
    max_data_staleness_seconds=60,
    dedup_window_seconds=5,

    # Emergency
    kill_switch_enabled=False,
)
```

## Shadow Mode

Always start in shadow mode:

```python
config = LiveRiskConfig(shadow_mode=True)
```

In shadow mode:
- Orders are logged but not executed
- All risk checks still run
- Perfect for strategy validation

## Position Limits

Control exposure per position:

```python
config = LiveRiskConfig(
    max_position_value=25_000,  # Max $ per position
    max_shares=1000,            # Max shares per position
    max_positions=10,           # Max concurrent positions
)
```

## Order Limits

Prevent oversized orders:

```python
config = LiveRiskConfig(
    max_order_value=10_000,  # Max $ per order
)
```

## Loss Limits

Stop trading on excessive losses:

```python
config = LiveRiskConfig(
    max_daily_loss=2_000,      # Stop after $2K daily loss
    max_drawdown_pct=0.10,     # Stop at 10% drawdown
)
```

## Safety Checks

Prevent common errors:

```python
config = LiveRiskConfig(
    max_price_deviation_pct=0.05,    # Fat finger protection
    max_data_staleness_seconds=60,   # Stale data protection
    dedup_window_seconds=5,          # Duplicate order prevention
)
```

## Kill Switch

Emergency halt capability:

```python
config = LiveRiskConfig(
    kill_switch_enabled=True,
)

# To activate kill switch:
broker.activate_kill_switch()
```

## SafeBroker

The `SafeBroker` wraps any broker with risk checks:

```python
from ml4t.live import IBBroker, SafeBroker, LiveRiskConfig

config = LiveRiskConfig(shadow_mode=True)
broker = SafeBroker(IBBroker(), config)
```

All orders pass through risk validation before execution.

## Error Handling

```python
from ml4t.live import RiskLimitExceeded

try:
    broker.submit_order("AAPL", 10000)  # Too many shares
except RiskLimitExceeded as e:
    print(f"Risk limit violated: {e}")
```

## Recommended Progression

1. **Shadow Mode**: Validate strategy logic
2. **Paper Trading**: Test with simulated orders
3. **Small Live**: Start with minimal position sizes
4. **Full Live**: Gradually increase limits
