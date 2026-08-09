# Data Feeds

`AlpacaDataFeed`, `IBDataFeed`, `OKXFundingFeed`, and `BarAggregator` emit the shared
`ml4t.specs.MarketEvent` contract. `LiveEngine` validates event timing and adapts each event to the
existing `on_data(timestamp, data, context, broker)` strategy callback. Alpaca, IB, generic CCXT,
and DataBento feeds remain experimental and require explicit opt-in.

| Feed | Event kinds | Event time | Completion | Sequence capability | Maximum age | Queue |
| --- | --- | --- | --- | --- | --- | --- |
| `AlpacaDataFeed` | bar, quote, trade | provider UTC timestamp | bars complete; quotes and trades evolving | provider sequence or explicit unavailable evidence | 120s bars; 30s quotes/trades | 1,024 |
| `IBDataFeed` | quote, trade | provider UTC time when present; otherwise receipt time | evolving snapshot | explicit unavailable evidence | 5s | 1,024 |
| `OKXFundingFeed` | bar, funding | candle open time; local receipt time for observed funding | provider candle flag; funding complete | candle/funding schedule identity or explicit unavailable evidence | two intervals plus poll delay | 256 |
| `BarAggregator` | bar | UTC interval start | complete after interval end; evolving when shutdown flushes the current interval | propagated gap or explicit unavailable evidence | engine setting | 256 |

Every event has separate `event_time` and `receipt_time`. The engine adds `processing_time` under
`context["_market_event"]`, together with event kind, completion, source, provider sequence, and gap
evidence. Naive or non-UTC timestamps, stale events, invalid OHLC ranges, nonpositive prices,
negative sizes, crossed quotes, and non-finite values are rejected before strategy dispatch.

## Overload And Reconnect Safety

Supported feeds halt instead of dropping, coalescing, or blocking indefinitely. Set
`queue_capacity` when a default does not match the workload. If a queue fills, pending events are
discarded, `FeedOverflowError` reports the rejected event and queue state with gap evidence, and
`LiveEngine` records `feed_safety_halt` before cleanup. No pending event is dispatched after the
overflow.

Capacity, occupancy, high-water mark, overflow count, oldest-event lag, and terminal state are
available under `feed.stats["queue"]` and `engine.stats["feed"]`. Accepted, duplicate, and rejected
continuity state appears under `engine.stats["continuity"]`.

The engine retains the last accepted event identity across automatic recovery. It skips an exact
replay. An older sequence, backwards event time, explicit gap, conflicting completed bar, or stale
snapshot halts before another callback. A feed without provider continuity evidence may run
normally, but it cannot make a new causal decision after reconnect. A sequence value alone is not
evidence that no records were missed. Treat the halt as an operator reconciliation point.

## Choosing A Feed

| Feed | Status | Best for | Typical pairing |
| --- | --- | --- | --- |
| `AlpacaDataFeed` | experimental | Alpaca-native equities and crypto | custom evaluation |
| `IBDataFeed` | experimental | direct market data from TWS or IB Gateway | custom evaluation |
| `OKXFundingFeed` | stable-supported | perpetual-swap strategies that depend on funding context | funding-rate and perp strategies |
| `BarAggregator` | stable-supported | converting tick or sub-minute feeds into strategy bars | custom typed feeds |
| `DataBentoFeed` | experimental | schema-limited replay and live evaluation | deliberate custom validation |
| `CryptoFeed` | experimental | generic CCXT evaluation | deliberate custom validation |

## AlpacaDataFeed

```python
from ml4t.live import AlpacaDataFeed

feed = AlpacaDataFeed(
    api_key="...",
    secret_key="...",
    symbols=["AAPL", "BTC/USD"],
    data_type="bars",  # "bars", "quotes", or "trades"
    feed="iex",        # "iex" or "sip"
    queue_capacity=1024,
    experimental=True,
)
```

Use this feed for Alpaca-native stocks and crypto. Stock symbols and `.../USD` crypto symbols can be mixed in one feed. The feed itself does not implement a standalone reconnect loop; use `LiveEngine(auto_recover=True)` if you want watchdog-driven restart behavior.
It remains experimental because bars and quotes do not consistently provide a provider sequence,
so continuity across reconnect cannot be established.

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
    queue_capacity=1024,
    experimental=True,
)
```

`IBDataFeed` emits separate trade and quote events. `LiveEngine` adapts a trade to
`{symbol: {"price", "size"}}` and a quote to bid/ask fields plus a midpoint `price`. Wrap the feed in
`BarAggregator` if your strategy expects OHLCV bars. The feed does not own a reconnect loop;
watchdog-driven stop/restart belongs in `LiveEngine` when enabled.
Pending-ticker snapshots can lack provider event time and sequence. The adapter then uses receipt
time and reports sequence unavailability, which is insufficient for stable continuity guarantees.

## Experimental Feeds

`AlpacaDataFeed`, `IBDataFeed`, `DataBentoFeed`, and `CryptoFeed` are not part of the stable support
contract. Construction fails unless `experimental=True` is passed, then emits
`ExperimentalFeedWarning` with that adapter's missing guarantees. Do not treat their public imports
as support claims.

### DataBentoFeed

Historical replay:

```python
from ml4t.live import DataBentoFeed

feed = DataBentoFeed.from_file(
    "data/ES_202401.dbn",
    symbols=["ES.FUT"],
    replay_speed=10.0,
    experimental=True,
)
```

Live streaming:

```python
feed = DataBentoFeed.from_live(
    api_key="...",
    dataset="GLBX.MDP3",
    schema="ohlcv-1s",
    symbols=["ES.c.0", "NQ.c.0"],
    experimental=True,
)
```

`DataBentoFeed` requires the `ml4t-live[experimental]` package extra. Its deterministic tests cover
only typed OHLCV, trade, and top-of-book records; this does not qualify the service or its other
schemas.

### CryptoFeed

```python
from ml4t.live import CryptoFeed

feed = CryptoFeed(
    exchange="binance",
    symbols=["BTC/USDT", "ETH/USDT"],
    timeframe="1m",
    stream_ohlcv=True,
    experimental=True,
)
```

`CryptoFeed` uses `ccxt.pro` when available and otherwise uses the asynchronous CCXT REST client.
It never selects synchronous CCXT under async code. For a candle batch, every candle before the
newest is `complete` and the newest is `evolving`. A changed evolving revision emits again, and the
same timestamp emits once more when a later batch proves it complete.

## OKXFundingFeed

```python
from ml4t.live import OKXFundingFeed

feed = OKXFundingFeed(
    symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
    timeframe="1m",  # also supports "1H", "4H", and "1D"
    poll_interval_seconds=5.0,
    queue_capacity=256,
)
```

`OKXFundingFeed` emits candle and funding records as separate events. `fundingTime` identifies the
scheduled settlement and appears in metadata; it is not used as the occurrence time of a rate that
was observed earlier. Minute granularity is supported, and candle timestamps align to UTC minute
boundaries. Identical complete candles and identical evolving revisions emit once.

## BarAggregator

Use `BarAggregator` to convert tick or sub-minute feeds into larger bars:

```python
from ml4t.live import BarAggregator

feed = BarAggregator(
    source_feed=raw_feed,
    bar_size_minutes=1,
    flush_timeout_seconds=2.0,
    queue_capacity=256,
)
```

Optional symbol filtering:

```python
feed = BarAggregator(raw_feed, bar_size_minutes=5, assets=["SPY", "QQQ"])
```

The aggregator emits one event per asset so sparse symbols retain independent interval boundaries.
An elapsed interval emits once as `complete`. If shutdown occurs before the current interval ends,
its buffered value emits once as `evolving`. Late input cannot reopen an already completed interval.

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

- Use `AlpacaDataFeed` for experimental evaluation of Alpaca-native stocks and crypto.
- Use `IBDataFeed` for experimental evaluation of direct TWS or IB Gateway market data.
- Use `OKXFundingFeed` for perpetual-swap strategies that depend on funding-rate context.
- Use `BarAggregator` when your upstream feed is tick-oriented but your strategy expects bars.
- Pass `experimental=True` only after accepting the limitations reported by an experimental feed.
