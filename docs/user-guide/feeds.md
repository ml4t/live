# Data Feeds

`ml4t-live` exposes five primary feed classes plus `BarAggregator` for resampling tick-style feeds.
Each feed yields `(timestamp, data, context)` tuples through `DataFeedProtocol`.

## Choosing A Feed

| Feed | Best for | Typical pairing |
| --- | --- | --- |
| `AlpacaDataFeed` | Alpaca-native equities and crypto | `AlpacaBroker` |
| `IBDataFeed` | direct market data from TWS or IB Gateway | `IBBroker` |
| `DataBentoFeed` | replay, institutional futures data, parity testing | replay workflows, validation |
| `CryptoFeed` | exchange-agnostic crypto streaming through CCXT | custom crypto execution stacks |
| `OKXFundingFeed` | perpetual-swap strategies that depend on funding context | funding-rate and perp strategies |
| `BarAggregator` | converting tick or sub-minute feeds into strategy bars | `IBDataFeed`, custom feeds |

## AlpacaDataFeed

```python
from ml4t.live import AlpacaDataFeed

feed = AlpacaDataFeed(
    api_key="...",
    secret_key="...",
    symbols=["AAPL", "BTC/USD"],
    data_type="bars",  # "bars", "quotes", or "trades"
    feed="iex",        # "iex" or "sip"
)
```

Use this feed for Alpaca-native stocks and crypto. Stock symbols and `.../USD` crypto symbols can be mixed in one feed. The feed itself does not implement a standalone reconnect loop; use `LiveEngine(auto_recover=True)` if you want watchdog-driven restart behavior.

## IBDataFeed

```python
from ml4t.live import IBBroker, IBDataFeed

broker = IBBroker(port=7497)
await broker.connect()

feed = IBDataFeed(
    broker.ib,
    symbols=["SPY", "QQQ"],
    exchange="SMART",
    currency="USD",
    tick_throttle_ms=1000,
)
```

`IBDataFeed` emits tick-style updates shaped like `{symbol: {"price", "size"}}`. Wrap it in `BarAggregator` if your strategy expects OHLCV bars. The feed does not own a reconnect loop; watchdog-driven stop/restart belongs in `LiveEngine` when enabled.

## DataBentoFeed

Historical replay:

```python
from ml4t.live import DataBentoFeed

feed = DataBentoFeed.from_file(
    "data/ES_202401.dbn",
    symbols=["ES.FUT"],
    replay_speed=10.0,
)
```

Live streaming:

```python
feed = DataBentoFeed.from_live(
    api_key="...",
    dataset="GLBX.MDP3",
    schema="ohlcv-1s",
    symbols=["ES.c.0", "NQ.c.0"],
)
```

`DataBentoFeed` requires the optional `databento` package.

## CryptoFeed

```python
from ml4t.live import CryptoFeed

feed = CryptoFeed(
    exchange="binance",
    symbols=["BTC/USDT", "ETH/USDT"],
    timeframe="1m",
    stream_ohlcv=True,
)
```

`CryptoFeed` uses `ccxt` or `ccxt.pro` depending on availability and supports both trade streaming and OHLCV streaming.

## OKXFundingFeed

```python
from ml4t.live import OKXFundingFeed

feed = OKXFundingFeed(
    symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
    timeframe="1m",  # also supports "1H", "4H", and "1D"
    poll_interval_seconds=5.0,
)
```

`OKXFundingFeed` combines OHLCV bars from `/api/v5/market/candles` with funding-rate context from `/api/v5/public/funding-rate`. Minute granularity is supported, and emitted timestamps align to UTC minute boundaries.

## BarAggregator

Use `BarAggregator` to convert tick or sub-minute feeds into larger bars:

```python
from ml4t.live import BarAggregator

feed = BarAggregator(
    source_feed=raw_feed,
    bar_size_minutes=1,
    flush_timeout_seconds=2.0,
)
```

Optional symbol filtering:

```python
feed = BarAggregator(raw_feed, bar_size_minutes=5, assets=["SPY", "QQQ"])
```

If you need lower-level aggregation state, `BarBuffer` is also part of the public surface and appears
in the [API Reference](../api/index.md).

## Using a Feed With LiveEngine

```python
engine = LiveEngine(strategy, safe_broker, feed)
await engine.connect()
await engine.run()
```

`LiveEngine.connect()` starts the feed for you, so normal engine usage does not require a manual
`feed.start()` call. Broker connection and feed startup are one transaction: a partial failure stops
the feed if startup was attempted and disconnects the broker. If you configure `auto_recover=True`,
the engine watchdog can stop and restart the broker/feed pair after `feed_silent` or
`broker_disconnected` events without repeating strategy startup callbacks.

## Choosing a Feed

- Use `AlpacaDataFeed` for Alpaca-native stocks and crypto.
- Use `IBDataFeed` for direct market data from TWS or IB Gateway.
- Use `DataBentoFeed` for replay or institutional market-data workflows.
- Use `CryptoFeed` for exchange-agnostic crypto streaming through CCXT.
- Use `OKXFundingFeed` for perpetual-swap strategies that depend on funding-rate context.
- Use `BarAggregator` when your upstream feed is tick-oriented but your strategy expects bars.
