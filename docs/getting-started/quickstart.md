# Quickstart

Run your first live trading strategy in 5 minutes.

## Step 1: Shadow Mode (Recommended)

Always start in shadow mode - orders are logged but not executed:

```python
from ml4t.live import LiveEngine, IBBroker, IBDataFeed, SafeBroker, LiveRiskConfig
from ml4t.backtest import Strategy

class SimpleStrategy(Strategy):
    def on_data(self, timestamp, data, context, broker):
        if data["close"] > data["sma_20"]:
            broker.submit_order("AAPL", 100)

# Shadow mode - no real orders!
config = LiveRiskConfig(shadow_mode=True)
broker = SafeBroker(IBBroker(), config)
feed = IBDataFeed(symbols=["AAPL"])

engine = LiveEngine(SimpleStrategy(), broker, feed)
await engine.connect()
await engine.run()
```

## Step 2: Paper Trading

Graduate to paper trading with simulated orders:

```python
config = LiveRiskConfig(
    shadow_mode=False,  # Real orders to paper account
    max_position_value=25_000,
    max_daily_loss=2_000,
)
```

## Step 3: Live Trading (Careful!)

Only after thorough testing:

```python
config = LiveRiskConfig(
    shadow_mode=False,
    max_position_value=10_000,
    max_order_value=5_000,
    max_daily_loss=1_000,
)

# Connect to live IB (port 7496)
broker = SafeBroker(
    IBBroker(port=7496),  # Live port
    config
)
```

## Risk Controls

Configure comprehensive risk limits:

```python
config = LiveRiskConfig(
    # Mode
    shadow_mode=True,

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
    max_price_deviation_pct=0.05,  # Fat finger protection
    max_data_staleness_seconds=60,
    dedup_window_seconds=5,

    # Emergency
    kill_switch_enabled=False,
)
```

## Error Handling

```python
from ml4t.live import BrokerConnectionError, RiskLimitExceeded

try:
    await engine.run()
except BrokerConnectionError:
    print("Lost connection to broker")
except RiskLimitExceeded as e:
    print(f"Risk limit violated: {e}")
```

## Next Steps

- [Risk Controls](../user-guide/risk.md) - Detailed risk configuration
- [Brokers](../user-guide/brokers.md) - Broker-specific setup
- [Data Feeds](../user-guide/feeds.md) - Market data options
