# Installation

## Requirements

- Python 3.12, 3.13, or 3.14
- A supported broker or data source for live use
- `ml4t-backtest` is installed automatically as a package dependency

## Install From PyPI

```bash
uv add ml4t-live
```

## Optional Add-Ons

`ml4t-live` installs the beta-supported broker/feed stack used by the package. The DataBento SDK is
available only through the bounded optional extra for deliberate experimental evaluation:

```bash
uv add 'ml4t-live[experimental]'
```

## Install From Source

```bash
git clone https://github.com/ml4t/live.git
cd live
uv sync --all-extras --dev
```

## Broker Setup

### Interactive Brokers

1. Install and launch TWS or IB Gateway
2. Enable API access in the IB settings
3. Use port `7497` for paper trading or `7496` for live trading

```python
from ml4t.live import IBBroker

broker = IBBroker(
    host="127.0.0.1",
    port=7497,
    client_id=1,
)
```

### Alpaca

1. Create an Alpaca account
2. Generate API credentials
3. Start with `paper=True`

```python
from ml4t.live import AlpacaBroker

broker = AlpacaBroker(
    api_key="YOUR_API_KEY",
    secret_key="YOUR_SECRET_KEY",
    paper=True,
)
```

## Verify Installation

```python
from ml4t.live import (
    AlpacaBroker,
    AlpacaDataFeed,
    BarAggregator,
    IBBroker,
    LiveEngine,
    LiveRiskConfig,
    SafeBroker,
)

print("ml4t-live imports succeeded")
```

## What To Do Next

After installation, the recommended first run is:

1. Follow the [Quickstart](quickstart.md) in `shadow_mode=True`.
2. Read [Backtest to Live](../user-guide/backtest-to-live.md) if you are porting an existing strategy.
3. Configure the [broker](../user-guide/brokers.md) and [feed](../user-guide/feeds.md) pages that match your deployment path.
4. Use the [Book Guide](../book-guide/index.md) to map chapter examples back to the library APIs.
