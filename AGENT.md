# ML4T Live - Agent Reference

> **Quick Start**: `from ml4t.live import LiveEngine, SafeBroker, LiveRiskConfig`

## Purpose
Live trading platform enabling zero-code migration from backtesting. Copy-paste your Strategy class from ml4t-backtest to live trading with no changes.

## Installation
```bash
pip install ml4t-live ml4t-backtest
```

## Core Architecture

```
LiveEngine (async orchestration)
    │
    ├── ThreadSafeBrokerWrapper (sync/async bridge)
    │       └── Strategy.on_data() (sync, matches backtest API)
    │
    ├── SafeBroker (risk layer)
    │       └── IBBroker / AlpacaBroker (async broker)
    │
    └── DataFeed (market data)
            └── IBFeed / DatabentoFeed / CryptoFeed
```

## Key Concept: Zero-Code Migration

```python
# This exact Strategy class works in BOTH backtest and live:
class MyStrategy(Strategy):
    def on_data(self, timestamp, data, context, broker):
        if data["close"] > data["sma_20"]:
            broker.submit_order("AAPL", 100)
```

## Basic Usage

### Shadow Mode (Recommended First)
```python
from ml4t.live import LiveEngine, IBBroker, IBDataFeed, SafeBroker, LiveRiskConfig

# Always start in shadow mode (no real orders!)
config = LiveRiskConfig(shadow_mode=True)
broker = SafeBroker(IBBroker(), config)
feed = IBDataFeed(symbols=["AAPL", "MSFT"])

engine = LiveEngine(my_strategy, broker, feed)
await engine.connect()
await engine.run()
```

### Paper Trading
```python
config = LiveRiskConfig(
    shadow_mode=False,  # Real orders to paper account
    max_position_value=25_000,
    max_daily_loss=2_000,
)
```

### Live Trading (Careful!)
```python
config = LiveRiskConfig(
    shadow_mode=False,
    max_position_value=10_000,
    max_order_value=5_000,
    max_daily_loss=1_000,
    kill_switch_enabled=False,  # Enable if emergency stop needed
)
```

## Available Brokers

| Broker | Class | Notes |
|--------|-------|-------|
| Interactive Brokers | `IBBroker` | Via ib_async, requires TWS/Gateway |
| Alpaca | `AlpacaBroker` | Paper + live, easy API keys |

## Available Data Feeds

| Feed | Class | Notes |
|------|-------|-------|
| Interactive Brokers | `IBDataFeed` | Real-time quotes via IB |
| Databento | `DatabentoFeed` | Institutional-grade streaming |
| Crypto | `CryptoFeed` | Exchange WebSocket feeds |
| Alpaca | `AlpacaFeed` | Alpaca market data |
| OKX | `OKXFeed` | OKX exchange data |

## Risk Controls (LiveRiskConfig)

```python
config = LiveRiskConfig(
    # Mode
    shadow_mode=True,              # Log but don't execute

    # Position Limits
    max_position_value=25_000,     # Max value per position
    max_shares=1000,               # Max shares per position
    max_positions=10,              # Max concurrent positions

    # Order Limits
    max_order_value=10_000,        # Max single order value

    # Loss Limits
    max_daily_loss=2_000,          # Daily loss limit
    max_drawdown_pct=0.10,         # 10% max drawdown

    # Safety Checks
    max_price_deviation_pct=0.05,  # 5% fat finger protection
    max_data_staleness_seconds=60, # Reject stale data
    dedup_window_seconds=5,        # Prevent duplicate orders

    # Emergency
    kill_switch_enabled=False,     # Halt all trading
)
```

## Key Classes

### LiveEngine
- Async orchestration of strategy, broker, and data feed
- Handles signal interrupts (SIGINT, SIGTERM)
- Error recovery and reconnection

### SafeBroker
- Wraps broker with risk checks
- Enforces all LiveRiskConfig limits
- Logs all order attempts

### ThreadSafeBrokerWrapper
- Bridges sync Strategy.on_data() to async broker
- Uses `run_coroutine_threadsafe()` internally

### VirtualPortfolio
- Tracks shadow positions when shadow_mode=True
- Prevents infinite buy loops
- State persisted to JSON

### BarAggregator
- Converts ticks to OHLCV bars
- Timeout-based flush for market close
- BarBuffer for multi-asset sync

## Safety Guidelines

1. **Always start with shadow_mode=True**
2. **Graduate to paper trading before live**
3. **Use conservative risk limits**
4. **Set max_daily_loss to limit damage**
5. **Test signal handling (Ctrl+C)**
6. **This is NOT a substitute for professional trading systems**

## Error Handling

```python
try:
    await engine.run()
except BrokerConnectionError:
    # Handle connection issues
except RiskLimitExceeded as e:
    # Handle risk violations
    print(f"Risk limit: {e}")
```

## Dependencies
- ml4t-backtest (Strategy, Order, Position)
- ib-async (Interactive Brokers API)
- asyncio (async/await infrastructure)
