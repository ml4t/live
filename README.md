# ml4t-live

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![PyPI](https://img.shields.io/pypi/v/ml4t-live)](https://pypi.org/project/ml4t-live/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Live trading platform with zero-code migration from backtest to production.

## Part of the ML4T Library Ecosystem

This library is one of five interconnected libraries supporting the machine learning for trading workflow described in [Machine Learning for Trading](https://mlfortrading.io):

![ML4T Library Ecosystem](docs/images/ml4t_ecosystem_workflow_print.jpeg)

Each library addresses a distinct stage: data infrastructure, feature engineering, signal evaluation, strategy backtesting, and live deployment.

## What This Library Does

Deploying a backtested strategy to live markets requires careful handling of async broker connections, risk limits, and testing infrastructure. ml4t-live provides:

- The same Strategy class used in ml4t-backtest works unchanged in production
- Two broker integrations: Interactive Brokers (TWS/Gateway) and Alpaca (stocks + crypto)
- Six data feeds: Alpaca, IB, Databento, CCXT (100+ crypto exchanges), OKX
- Shadow mode for testing without placing real orders (VirtualPortfolio tracking)
- 16-parameter risk configuration: position limits, order limits, loss limits, price protection
- Kill switch with crash-safe state persistence (atomic JSON writes)
- Async architecture with thread-safe sync bridge for strategy callbacks

The goal is gradual deployment: shadow mode first, then paper trading, then live with small positions.

![ml4t-live Architecture](docs/images/ml4t_live_architecture_print.jpeg)

## Installation

```bash
pip install ml4t-live
```

## Quick Start

```python
from ml4t.backtest import Strategy, OrderSide
from ml4t.live import LiveEngine, LiveRiskConfig, SafeBroker
from ml4t.live.brokers.alpaca import AlpacaBroker
from ml4t.live.feeds.alpaca_feed import AlpacaDataFeed
import asyncio

# Same strategy class from backtesting
class MyStrategy(Strategy):
    def on_data(self, timestamp, data, context, broker):
        if not broker.get_position('SPY'):
            broker.submit_order('SPY', 10, side=OrderSide.BUY)

async def main():
    broker = AlpacaBroker(api_key="...", secret_key="...", paper=True)
    feed = AlpacaDataFeed(api_key="...", secret_key="...", symbols=["SPY"])

    config = LiveRiskConfig(
        shadow_mode=True,           # No real orders
        max_position_value=50_000,
    )
    safe = SafeBroker(broker, config)

    engine = LiveEngine(MyStrategy(), safe, feed)
    await engine.connect()

    try:
        await engine.run()
    finally:
        await engine.stop()

asyncio.run(main())
```

## Broker Integrations

### Alpaca

Stocks and crypto with paper trading by default:

```python
from ml4t.live.brokers.alpaca import AlpacaBroker

broker = AlpacaBroker(
    api_key="...",
    secret_key="...",
    paper=True,       # Paper trading (default)
)
await broker.connect()
```

### Interactive Brokers

Full market access via TWS or IB Gateway:

```python
from ml4t.live.brokers.ib import IBBroker

broker = IBBroker(port=7497)  # TWS paper port
# broker = IBBroker(port=7496)  # TWS live port

await broker.connect()
print(f"Connected: {broker.is_connected}")
```

Requirements:
- IB TWS or Gateway running
- API connections enabled in TWS settings
- Paper trading account for initial testing

## Data Feeds

| Feed | Source | Coverage |
|------|--------|----------|
| `AlpacaDataFeed` | Alpaca | US stocks + crypto, real-time bars/quotes/trades |
| `IBDataFeed` | Interactive Brokers | Multi-asset tick-by-tick data |
| `DataBentoFeed` | Databento | Historical replay + real-time streaming |
| `CryptoFeed` | CCXT | 100+ crypto exchanges (Binance, Coinbase, Kraken, ...) |
| `OKXFundingFeed` | OKX | Perpetual swaps with funding rates |
| `BarAggregator` | Any feed | Multi-feed aggregation + bar assembly |

```python
from ml4t.live.feeds.alpaca_feed import AlpacaDataFeed
from ml4t.live.feeds.crypto_feed import CryptoFeed

# Stock + crypto via Alpaca
feed = AlpacaDataFeed(
    api_key="...", secret_key="...",
    symbols=["AAPL", "BTC/USD"],
    feed="iex",          # "iex" (free) or "sip" (premium)
)

# Crypto via CCXT (any of 100+ exchanges)
feed = CryptoFeed(
    exchange="binance",
    symbols=["BTC/USDT", "ETH/USDT"],
    timeframe="1m",
)
```

## Risk Configuration

`LiveRiskConfig` controls all safety parameters. Wrap any broker with `SafeBroker` to enforce them:

```python
from ml4t.live import LiveRiskConfig, SafeBroker

config = LiveRiskConfig(
    # Shadow mode
    shadow_mode=True,                   # Virtual orders only (no real execution)

    # Position limits
    max_position_value=50_000,          # Max $ per position
    max_position_shares=1000,           # Max shares per position
    max_total_exposure=200_000,         # Max total $ across all positions
    max_positions=20,                   # Max number of positions

    # Order limits
    max_order_value=10_000,             # Max $ per order
    max_order_shares=500,               # Max shares per order
    max_orders_per_minute=10,           # Rate limiting

    # Loss limits
    max_daily_loss=5_000,               # Stop trading if exceeded
    max_drawdown_pct=0.05,              # Stop if 5% drawdown

    # Price protection
    max_price_deviation_pct=0.05,       # Fat finger: reject if >5% from market
    max_data_staleness_seconds=60,      # Reject if data older than 60s
    dedup_window_seconds=1.0,           # Block duplicate orders within 1s

    # Asset restrictions
    allowed_assets={"SPY", "QQQ"},      # Whitelist (empty = allow all)
)

safe_broker = SafeBroker(broker, config)
```

## Safety System

### Kill Switch

When drawdown exceeds `max_drawdown_pct`, the kill switch activates and blocks all new orders. The state persists across process restarts:

```python
config = LiveRiskConfig(
    kill_switch_enabled=True,
    max_drawdown_pct=0.05,
    state_file=".ml4t_risk_state.json",  # Atomic JSON writes
)
```

### Virtual Portfolio

Shadow mode tracks positions internally without broker interaction:

```python
from ml4t.live import VirtualPortfolio

portfolio = VirtualPortfolio(initial_cash=100_000)
# SafeBroker uses this automatically when shadow_mode=True
```

### State Persistence

Risk state survives process crashes via atomic file writes:

- `daily_loss` - Cumulative daily loss
- `orders_placed` - Orders placed today
- `high_water_mark` - Session high equity
- `kill_switch_activated` - Persists until manually reset

## Deployment Progression

1. **Shadow Mode** (1-2 weeks): Verify logic without real orders
2. **Paper Trading** (2-4 weeks): Test with paper account
3. **Live Micro** (1-2 weeks): Small positions ($100-500)
4. **Live Small** (ongoing): Gradual size increase

## Strategy Compatibility

The same Strategy class works in both environments:

```python
from ml4t.backtest import Strategy

class MyStrategy(Strategy):
    def on_data(self, timestamp, data, context, broker):
        # This code runs identically in backtest and live
        pass

# Backtest
from ml4t.backtest import Engine
result = Engine(feed, MyStrategy(), config).run()

# Live
from ml4t.live import LiveEngine
await LiveEngine(MyStrategy(), safe_broker, live_feed).run()
```

## Documentation

- [Installation](docs/getting-started/installation.md) — setup instructions
- [Quick Start](docs/getting-started/quickstart.md) — first live strategy
- [Brokers](docs/user-guide/brokers.md) — IB and Alpaca setup
- [Data Feeds](docs/user-guide/feeds.md) — 6 feed types
- [Risk Management](docs/user-guide/risk.md) — LiveRiskConfig and SafeBroker

## Technical Characteristics

- **Async/sync bridge**: Sync strategy callbacks work with async broker connections via `ThreadSafeBrokerWrapper`
- **Thread-safe**: Strategy runs in worker thread, broker I/O on async event loop
- **Protocol-based**: `BrokerProtocol`, `AsyncBrokerProtocol`, `DataFeedProtocol` for extensibility
- **Virtual portfolio**: Shadow mode tracks positions without broker interaction
- **Atomic state**: Risk state persisted via POSIX-atomic file writes (crash-safe)
- **Rate limiting**: Built-in protection against order flooding
- **Type-safe**: Full type annotations throughout

## Related Libraries

- **ml4t-data**: Market data acquisition and storage
- **ml4t-engineer**: Feature engineering and technical indicators
- **ml4t-diagnostic**: Signal evaluation and statistical validation
- **ml4t-backtest**: Event-driven backtesting

## Development

```bash
git clone https://github.com/ml4t/ml4t-live.git
cd ml4t-live
uv sync
uv run pytest tests/ -q
uv run ty check
```

## Safety Notice

This library is designed for paper trading and educational purposes. When transitioning to live trading:

- Always start with `shadow_mode=True`
- Set conservative position and order limits
- Enable `kill_switch_enabled=True` with a reasonable `max_drawdown_pct`
- Monitor virtual vs real positions carefully
- Use the deployment progression above

## License

MIT License - see [LICENSE](LICENSE) for details.
