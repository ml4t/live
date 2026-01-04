# ML4T Live

Live trading platform enabling **zero-code migration** from backtesting to live trading.

## Overview

ML4T Live lets you copy-paste your Strategy class from ml4t-backtest to live trading with **no code changes**. The same strategy works in both environments.

## Key Concept: Zero-Code Migration

```python
# This exact Strategy class works in BOTH backtest and live:
class MyStrategy(Strategy):
    def on_data(self, timestamp, data, context, broker):
        if data["close"] > data["sma_20"]:
            broker.submit_order("AAPL", 100)
```

## Architecture

```
LiveEngine (async orchestration)
    |
    +-- ThreadSafeBrokerWrapper (sync/async bridge)
    |       +-- Strategy.on_data() (sync, matches backtest API)
    |
    +-- SafeBroker (risk layer)
    |       +-- IBBroker / AlpacaBroker (async broker)
    |
    +-- DataFeed (market data)
            +-- IBFeed / DatabentoFeed / CryptoFeed
```

## Safety First

ML4T Live is designed with safety as the top priority:

1. **Shadow Mode**: Test strategies without real orders
2. **Paper Trading**: Validate with simulated orders
3. **Risk Limits**: Position, order, and loss limits
4. **Kill Switch**: Emergency halt capability

## Quick Example

```python
from ml4t.live import LiveEngine, IBBroker, IBDataFeed, SafeBroker, LiveRiskConfig

# Always start in shadow mode!
config = LiveRiskConfig(shadow_mode=True)
broker = SafeBroker(IBBroker(), config)
feed = IBDataFeed(symbols=["AAPL", "MSFT"])

engine = LiveEngine(my_strategy, broker, feed)
await engine.connect()
await engine.run()
```

## Installation

```bash
pip install ml4t-live ml4t-backtest
```

## Next Steps

- [Installation Guide](getting-started/installation.md) - Setup instructions
- [Quickstart](getting-started/quickstart.md) - Your first live strategy
- [Risk Controls](user-guide/risk.md) - Configure safety limits
- [API Reference](api/index.md) - Complete API documentation

## Part of the ML4T Library Suite

ML4T Live is the final stage in the ML4T workflow:

```
ml4t-data → ml4t-engineer → ml4t-diagnostic → ml4t-backtest → ml4t-live
```

## Disclaimer

**This library is designed for paper trading and educational purposes.**

- Always start with shadow_mode=True
- Graduate to paper trading before live
- Use small positions when going live
- Set conservative risk limits
- This is NOT a substitute for professional trading systems
