# ml4t-live

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Live trading platform enabling zero-code migration from backtest to production. Use the same `Strategy` class in both environments with comprehensive risk controls and shadow mode for testing.

## Features

- **Zero-Change Strategy Migration**: Copy-paste from backtest to live
- **Shadow Mode**: Test strategies without placing real orders
- **Risk Management**: Position limits, order limits, rate limiting
- **Interactive Brokers Integration**: Full async TWS/Gateway support
- **Bar Aggregation**: Convert tick data to minute bars
- **Thread-Safe**: Bridges sync strategies to async brokers

## Installation

```bash
pip install ml4t-live
```

## Quick Start

```python
from ml4t.backtest import Strategy, OrderSide
from ml4t.live import LiveEngine, LiveRiskConfig
from ml4t.live.brokers.ib import IBBroker
import asyncio

# Same strategy class used in backtesting
class MyStrategy(Strategy):
    def on_data(self, timestamp, data, context, broker):
        if not broker.get_position('SPY'):
            broker.submit_order('SPY', 10, side=OrderSide.BUY)

async def main():
    # Connect to IB (paper trading port)
    broker = IBBroker(port=7497)
    await broker.connect()

    # Create engine with shadow mode (no real orders)
    config = LiveRiskConfig(shadow_mode=True, max_position_value=50_000)
    engine = LiveEngine(broker, MyStrategy(), config)

    try:
        await engine.run()
    except KeyboardInterrupt:
        print("Stopping...")
    finally:
        await broker.disconnect()

asyncio.run(main())
```

**Shadow mode output:**
```
Bar 1: SPY close = $450.02
  -> Buying 10 shares of SPY (VIRTUAL - shadow mode)
Virtual position: +10 SPY @ $450.02
No real orders placed (shadow mode active)
```

## Architecture

```
LiveEngine (async orchestration)
├── DataFeed (real-time bars)
├── ThreadSafeBrokerWrapper (sync/async bridge)
│   └── IBBroker (or other broker implementation)
├── SafeBroker (risk controls)
│   ├── VirtualPortfolio (shadow mode)
│   └── RiskLimits (position/order limits)
└── Strategy (user logic - same as backtest)
```

### Key Modules

| Module | Purpose |
|--------|---------|
| `engine.py` | LiveEngine async orchestration |
| `wrappers.py` | ThreadSafeBrokerWrapper (sync/async bridge) |
| `safety.py` | Risk controls, VirtualPortfolio, SafeBroker |
| `brokers/` | Broker implementations (IB) |
| `feeds/` | Data feed implementations |

## Risk Configuration

```python
from ml4t.live import LiveRiskConfig

config = LiveRiskConfig(
    # Shadow mode (no real orders)
    shadow_mode=True,

    # Position limits
    max_position_value=50_000,      # Max value per position
    max_positions=10,               # Max number of positions

    # Order limits
    max_order_value=10_000,         # Max single order value
    max_orders_per_minute=10,       # Rate limiting

    # Daily limits
    max_daily_loss=5_000,           # Stop trading on daily loss
)
```

## Broker Integrations

### Interactive Brokers

```python
from ml4t.live.brokers.ib import IBBroker

# Paper trading
broker = IBBroker(port=7497)  # TWS paper port

# Live trading (use with caution)
broker = IBBroker(port=7496)  # TWS live port

await broker.connect()

# Check connection
print(f"Connected: {broker.is_connected}")
print(f"Account: {broker.account_id}")
```

**Prerequisites:**
- IB TWS or Gateway running
- API connections enabled in TWS settings
- Paper trading account for testing

## Shadow Mode

Shadow mode tracks positions virtually without placing real orders:

```python
config = LiveRiskConfig(shadow_mode=True)

# Orders are simulated
# Positions tracked in VirtualPortfolio
# Real broker not touched
```

**Safety Progression:**

1. **Shadow Mode** (1-2 weeks): Verify logic, no real orders
2. **Paper Trading** (2-4 weeks): Test with IB paper account
3. **Live Micro** (1-2 weeks): Tiny positions ($100-500)
4. **Live Small** (ongoing): Gradual size increase

## Bar Aggregation

Convert tick data to minute bars:

```python
from ml4t.live.feeds import BarAggregator

aggregator = BarAggregator(interval_seconds=60)

# Feed ticks
for tick in tick_stream:
    bar = aggregator.on_tick(tick)
    if bar is not None:
        # Complete bar ready
        strategy.on_bar(bar)
```

## Integration with ML4T Libraries

ml4t-live uses the same Strategy class as ml4t-backtest:

```python
from ml4t.backtest import Strategy

class MyStrategy(Strategy):
    def on_data(self, timestamp, data, context, broker):
        # Same code works in backtest and live
        pass

# Backtest
from ml4t.backtest import Engine
backtest_engine = Engine(feed, MyStrategy(), config)
result = backtest_engine.run()

# Live (same strategy)
from ml4t.live import LiveEngine
live_engine = LiveEngine(broker, MyStrategy(), risk_config)
await live_engine.run()
```

## Ecosystem

- **ml4t-data**: Market data acquisition and storage
- **ml4t-engineer**: Feature engineering and indicators
- **ml4t-diagnostic**: Statistical validation and evaluation
- **ml4t-backtest**: Event-driven backtesting
- **ml4t-live**: Live trading platform (this library)

## Testing

```bash
# Run all tests (378 tests)
uv run pytest tests/ -q

# Unit tests only
uv run pytest tests/unit/

# Integration tests (requires IB)
uv run pytest tests/integration/ -v --ib-port=7497

# Type checking
uv run ty check
```

## Development

```bash
git clone https://github.com/applied-ai/ml4t-live.git
cd ml4t-live

# Install with dev dependencies
uv sync

# Run tests
uv run pytest tests/ -q

# Type checking
uv run ty check

# Linting
uv run ruff check src/
```

## Safety Disclaimer

This library is designed for paper trading and educational purposes.

**Risk Management Best Practices:**
- Always start with `shadow_mode=True`
- Set conservative `max_position_value` and `max_order_value`
- Monitor virtual vs real positions carefully
- Use stop-losses and position limits
- This is NOT a substitute for professional trading systems

## Current Status

**Version**: 0.1.0a4

**Completed:**
- Core engine with async/sync bridging
- IB broker integration with TWS/Gateway
- Shadow mode and risk management
- Bar aggregation and data feeds
- 378 tests (unit + integration)

**Remaining:**
- Stress testing (memory leaks, long-running)
- Additional broker implementations (Alpaca)

## License

MIT License - see [LICENSE](LICENSE) for details.
