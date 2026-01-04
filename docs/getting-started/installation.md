# Installation

## Requirements

- Python 3.11 or higher
- ml4t-backtest (for Strategy class)
- ib-async (for Interactive Brokers)

## Install from PyPI

```bash
pip install ml4t-live ml4t-backtest
```

## Install from Source

```bash
git clone https://github.com/stefan-jansen/ml4t-live.git
cd ml4t-live
pip install -e .
```

## Broker Setup

### Interactive Brokers

1. Download TWS or IB Gateway from [Interactive Brokers](https://www.interactivebrokers.com/)
2. Enable API connections in TWS/Gateway settings
3. Configure port (default: 7497 for paper, 7496 for live)

```python
from ml4t.live import IBBroker

broker = IBBroker(
    host="127.0.0.1",
    port=7497,  # Paper trading port
    client_id=1,
)
```

### Alpaca

1. Sign up at [Alpaca](https://alpaca.markets/)
2. Get API keys from the dashboard

```python
from ml4t.live import AlpacaBroker

broker = AlpacaBroker(
    api_key="YOUR_API_KEY",
    secret_key="YOUR_SECRET_KEY",
    paper=True,  # Use paper trading
)
```

## Verify Installation

```python
from ml4t.live import LiveEngine, SafeBroker, LiveRiskConfig

print("ml4t-live installed successfully!")
```
