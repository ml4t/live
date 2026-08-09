# Quickstart

The recommended first run is shadow mode: your strategy executes through the live engine and risk
checks, but no real orders are sent to the broker.

## First Strategy

```python
import asyncio

from ml4t.backtest import Strategy
from ml4t.backtest.types import OrderSide
from ml4t.live import AlpacaBroker, AlpacaDataFeed, LiveEngine, LiveRiskConfig, SafeBroker


class BuyOnceStrategy(Strategy):
    def on_data(self, timestamp, data, context, broker):
        bar = data.get("SPY")
        if bar is None:
            return

        if broker.get_position("SPY") is None:
            broker.submit_order("SPY", 10, side=OrderSide.BUY)


async def main():
    broker = AlpacaBroker(api_key="...", secret_key="...", paper=True)
    feed = AlpacaDataFeed(
        api_key="...",
        secret_key="...",
        symbols=["SPY"],
        data_type="bars",
        experimental=True,
    )
    safe_broker = SafeBroker(
        broker,
        LiveRiskConfig(
            execution_mode="shadow",
            max_position_value=25_000,
            max_order_value=5_000,
        ),
    )

    engine = LiveEngine(BuyOnceStrategy(), safe_broker, feed)
    await engine.connect()

    try:
        await engine.run()
    finally:
        await engine.stop()


asyncio.run(main())
```

## Why This Works

- Your strategy stays synchronous, just like in `ml4t-backtest`
- `LiveEngine` runs broker/feed I/O asynchronously
- `LiveEngine` runs every lifecycle callback on one worker thread, so synchronous broker calls do
  not re-enter or block the async event loop
- `SafeBroker` enforces limits before any live order can be placed

## First-Run Checklist

Before you move past this example, confirm that:

1. the strategy receives bars from the feed you expect
2. orders appear in shadow mode instead of hitting the broker
3. positions and cash update through the virtual portfolio
4. you can stop and restart the engine cleanly

## Deployment Progression

1. Shadow mode: `execution_mode="shadow"`
2. Paper trading: `execution_mode="paper"` with paper broker credentials
3. Small live size: `execution_mode="live"` with conservative limits and low notional exposure
4. Gradual scale-up only after observing stable behavior

## Common Variations

### Experimental Interactive Brokers Feed

`IBDataFeed` needs a connected IB session object and explicit experimental opt-in:

```python
broker = IBBroker(port=7497)
await broker.connect()

feed = IBDataFeed(broker.ib, symbols=["SPY", "QQQ"], experimental=True)
```

### Aggregate Ticks Into Bars

```python
raw_feed = IBDataFeed(broker.ib, symbols=["SPY"], experimental=True)
feed = BarAggregator(raw_feed, bar_size_minutes=1, flush_timeout_seconds=2.0)
```

## Next Steps

- [Installation](installation.md)
- [Backtest to Live](../user-guide/backtest-to-live.md)
- [Risk Controls](../user-guide/risk.md)
- [CLI](../user-guide/cli.md)
- [Examples](../user-guide/examples.md)
- [Operator Guide](../user-guide/operator-guide.md)
- [Brokers](../user-guide/brokers.md)
- [Data Feeds](../user-guide/feeds.md)
- [Book Guide](../book-guide/index.md)
