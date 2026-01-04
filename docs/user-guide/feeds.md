# Data Feeds

Market data sources for live trading.

## Interactive Brokers Feed

Real-time data from IB:

```python
from ml4t.live import IBDataFeed

feed = IBDataFeed(
    symbols=["AAPL", "MSFT", "GOOGL"],
    host="127.0.0.1",
    port=7497,
)
```

### Subscription

```python
await feed.connect()
await feed.subscribe()

# Data arrives via callbacks
async for bar in feed:
    print(f"{bar.symbol}: {bar.close}")
```

## Databento Feed

High-quality market data:

```python
from ml4t.live import DatabentoFeed

feed = DatabentoFeed(
    api_key="YOUR_API_KEY",
    symbols=["AAPL", "MSFT"],
    schema="ohlcv-1m",  # 1-minute bars
)
```

## Crypto Feeds

### Binance

```python
from ml4t.live import BinanceFeed

feed = BinanceFeed(
    symbols=["BTCUSDT", "ETHUSDT"],
)
```

### Generic Crypto

```python
from ml4t.live import CryptoFeed

feed = CryptoFeed(
    exchange="binance",
    symbols=["BTC/USDT", "ETH/USDT"],
)
```

## Using with LiveEngine

```python
from ml4t.live import LiveEngine, IBBroker, IBDataFeed, SafeBroker, LiveRiskConfig

config = LiveRiskConfig(shadow_mode=True)
broker = SafeBroker(IBBroker(), config)
feed = IBDataFeed(symbols=["AAPL", "MSFT"])

engine = LiveEngine(my_strategy, broker, feed)
await engine.connect()
await engine.run()
```

## Bar Aggregation

The `BarAggregator` combines tick data into bars:

```python
from ml4t.live import BarAggregator

aggregator = BarAggregator(
    bar_size="1min",
    timeout_seconds=65,  # Flush incomplete bars
)
```

## Data Quality

The feed monitors data quality:

```python
from ml4t.live import LiveRiskConfig

config = LiveRiskConfig(
    max_data_staleness_seconds=60,  # Alert on stale data
)
```

## Error Handling

```python
from ml4t.live import DataFeedError

try:
    await feed.connect()
except DataFeedError:
    print("Failed to connect to data feed")
```
