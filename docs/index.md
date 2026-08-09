# ML4T Live

Run a lifecycle-v1 `Strategy` against real brokers and live market data, with `SafeBroker` risk
controls layered on top.

`ml4t-live` is the deployment layer in the ML4T stack. It sits after research and backtesting, where
the main problem is no longer "does this idea work?" but "can I run it safely against a broker without
preserving its portable decisions and controlling operational risk?" The library is built for staged rollout:
shadow mode first, then paper trading, then small live size under explicit limits.

<div class="grid cards" markdown>

-   :material-swap-horizontal:{ .lg .middle } __Versioned Strategy Portability__

    ---

    Keep the same `Strategy.on_data(...)` interface from `ml4t-backtest`.
    `LiveEngine` handles the async broker and feed runtime around it.
    [:octicons-arrow-right-24: Quickstart](getting-started/quickstart.md)

-   :material-shield-lock:{ .lg .middle } __SafeBroker Risk Controls__

    ---

    Position caps, order limits, duplicate-order protection, and a persistent kill switch.
    Start in shadow mode before you route anything real.
    [:octicons-arrow-right-24: Risk Controls](user-guide/risk.md)

-   :material-lan:{ .lg .middle } __Multiple Brokers And Feeds__

    ---

    Alpaca and Interactive Brokers for execution, plus replay and crypto feed options.
    Change infrastructure without forking strategy logic.
    [:octicons-arrow-right-24: Brokers](user-guide/brokers.md)

-   :material-book-open-variant:{ .lg .middle } __From Book To Production__

    ---

    The book develops strategies in research and backtest form.
    This library runs portable lifecycle and intent logic with explicit deployment controls.
    [:octicons-arrow-right-24: Book Guide](book-guide/index.md)

</div>

## What You Can Do With It Right Now

With `ml4t-live`, you can:

- take an existing `ml4t-backtest.Strategy` and run it in `LiveEngine`
- connect that strategy to Alpaca or Interactive Brokers
- replay or stream data through a live-style feed interface
- start in `execution_mode="shadow"` so orders are tracked but not routed
- add hard limits for order size, exposure, stale data, daily loss, and drawdown before going live
- inspect persisted state and broker reachability with `ml4t-live status`
- run bounded shadow sessions from the CLI before promoting to paper or live

If you do not yet have a validated strategy, start in `ml4t-backtest`. If you do have one, this is the
next layer.

## Where It Fits In The ML4T Stack

| Stage | Primary library | Practical question |
| --- | --- | --- |
| Data preparation | `ml4t-data` | Do I trust the raw and canonical inputs? |
| Features and labels | `ml4t-engineer` | Am I computing the right signals? |
| Diagnostics and validation | `ml4t-diagnostic` | Are the signals robust and statistically defensible? |
| Simulation and cost realism | `ml4t-backtest` | Does the strategy survive explicit execution assumptions? |
| Deployment and staged rollout | `ml4t-live` | Can I run the validated strategy against a real broker safely? |

## Strategy Portability

Strategy portability is a versioned contract. The strategy class can stay the same when it uses
only the shared lifecycle, information, intent, and broker boundaries. The engine and infrastructure
still change.

### Portable Strategy Logic

```python
from ml4t.backtest import Strategy
from ml4t.backtest.types import OrderSide

class BuyOnceStrategy(Strategy):
    def on_data(self, timestamp, data, context, broker):
        bar = data.get("SPY")
        if bar is None:
            return

        if broker.get_position("SPY") is None:
            broker.submit_order("SPY", 10, side=OrderSide.BUY)
```

### Backtest Run

```python
from ml4t.backtest import DataFeed, Engine

feed = DataFeed(prices_df=prices)
engine = Engine(feed=feed, strategy=BuyOnceStrategy())
result = engine.run()
```

### Live Run

```python
from ml4t.live import AlpacaBroker, AlpacaDataFeed, LiveEngine, LiveRiskConfig, SafeBroker

raw_broker = AlpacaBroker(api_key="...", secret_key="...", paper=True)
safe_broker = SafeBroker(raw_broker, LiveRiskConfig(execution_mode="shadow"))
feed = AlpacaDataFeed(api_key="...", secret_key="...", symbols=["SPY"], data_type="bars")

engine = LiveEngine(BuyOnceStrategy(), safe_broker, feed)
await engine.connect()
await engine.run()
```

Backtest and live are different systems. A portable strategy uses the same versioned callback and
canonical-intent contract in both. Outcome comparisons must still account for market data,
execution policy, venue capabilities, safety decisions, latency, and account state.

## Staged Rollout

`ml4t-live` is designed for staged deployment, not instant promotion from notebook to production.

### 1. Start In Shadow Mode

The strategy runs, risk checks run, and `VirtualPortfolio` tracks fills, but no real order is sent.

```python
safe_broker = SafeBroker(
    raw_broker,
    LiveRiskConfig(
        execution_mode="shadow",
        max_position_value=25_000,
        max_order_value=5_000,
        max_orders_per_minute=5,
    ),
)
```

### 2. Graduate To Paper Trading

The key config change is turning shadow mode off while still using paper broker credentials.

```python
safe_broker = SafeBroker(
    raw_broker,
    LiveRiskConfig(
        execution_mode="paper",
        max_position_value=25_000,
        max_order_value=5_000,
        max_orders_per_minute=5,
    ),
)
```

### 3. Go To Small Live Size

Keep limits tight enough that mistakes are survivable.

```python
safe_broker = SafeBroker(
    raw_broker,
    LiveRiskConfig(
        execution_mode="live",
        max_position_value=10_000,
        max_total_exposure=25_000,
        max_order_value=2_500,
        max_drawdown_pct=0.03,
        max_daily_loss=1_000,
    ),
)
```

### 4. Scale Only After Stable Monitoring

Promotion to larger live size should follow routine reconciliation, order-flow review, and operational
checks, not just a passing backtest.

## SafeBroker In Action

The safety model matters because this is a live trading library. `SafeBroker` is the main trust signal,
so it should be understood in concrete terms:

- Set `max_order_shares=100` and `max_order_value=5_000`. `SafeBroker` rejects any single order that
  would exceed 100 shares or $5,000 notional.
- Set `max_position_value=25_000` and `max_total_exposure=50_000`. `SafeBroker` blocks orders that
  would push one position or the overall book beyond those limits.
- Set `max_orders_per_minute=5`. A runaway loop that tries to fire ten orders in a burst gets stopped at
  the broker wrapper.
- Set `allowed_assets={"SPY", "QQQ"}`. Any attempt to trade an asset outside that set raises
  `RiskLimitError`.
- Set `max_drawdown_pct=0.05`. If portfolio equity drops 5% from the high-water mark, the kill switch
  activates and new orders are refused until you clear it.
- Leave `execution_mode="shadow"` selected during the first rollout phase. Orders are recorded and reflected in the
  virtual portfolio, but the real broker never sees them.

### Example Configuration

```python
from ml4t.live import LiveRiskConfig

config = LiveRiskConfig(
    max_position_value=25_000,
    max_position_shares=1_000,
    max_total_exposure=50_000,
    max_positions=10,
    max_order_value=5_000,
    max_order_shares=100,
    max_orders_per_minute=5,
    max_daily_loss=2_000,
    max_drawdown_pct=0.05,
    dedup_window_seconds=1.0,
    allowed_assets={"SPY", "QQQ"},
    execution_mode="shadow",
)
```

### What A Breach Looks Like

```python
from ml4t.live import RiskLimitError

try:
    await safe_broker.submit_order_async("SPY", 1_000)
except RiskLimitError as exc:
    print(f"order blocked: {exc}")
```

That failure mode is intentional. In live trading, "do nothing" is often the correct behavior when the
request is unsafe.

## Broker Comparison

| Broker | Asset Classes | Paper Trading | Live Trading | Best fit |
| --- | --- | --- | --- | --- |
| **Alpaca** | US equities, ETFs, Alpaca-supported crypto | Yes | Yes | fast setup, developer-friendly paper workflow |
| **Interactive Brokers** | global equities, options, futures, multi-asset brokerage accounts | Yes | Yes | broader asset coverage and existing IBKR setups |

See [Brokers](user-guide/brokers.md) for connection details and usage patterns.

## Data Feed Comparison

| Data Feed | Status | Source | Market Shape | Best for |
| --- | --- | --- | --- | --- |
| **AlpacaDataFeed** | beta-supported | Alpaca API | bars, quotes, trades | US equities and Alpaca crypto |
| **IBDataFeed** | beta-supported | TWS / IB Gateway | real-time ticks | IB-driven multi-asset execution |
| **OKXFundingFeed** | beta-supported | OKX public APIs | OHLCV plus funding context | perpetual futures and funding-rate strategies |
| **DataBentoFeed** | experimental opt-in | DataBento API or DBN replay | selected tick and bar schemas | custom evaluation only |
| **CryptoFeed** | experimental opt-in | asynchronous CCXT | trades and candles | custom evaluation only |

Use [Data Feeds](user-guide/feeds.md) for examples and feed-specific setup.

## Operator Surfaces

The library now includes a small CLI and a set of bounded operator-focused examples.
Use [CLI](user-guide/cli.md) for `status` and `shadow`, [Examples](user-guide/examples.md) for runnable scripts, and [Operator Guide](user-guide/operator-guide.md) for the recommended promotion workflow.

## Installation

```bash
uv add ml4t-live
```

Or:

```bash
pip install ml4t-live
```

Install `ml4t-live[experimental]` if you deliberately opt in to experimental `DataBentoFeed` use.

## Documentation Map

- [Installation](getting-started/installation.md) for environment setup and broker prerequisites
- [Quickstart](getting-started/quickstart.md) for the first shadow-mode run
- [Backtest to Live](user-guide/backtest-to-live.md) for the migration workflow
- [Risk Controls](user-guide/risk.md) for `SafeBroker` and rollout policy
- [Brokers](user-guide/brokers.md) for Alpaca and IB paths
- [Data Feeds](user-guide/feeds.md) for live and replay feed choices
- [Book Guide](book-guide/index.md) for chapter, notebook, and case-study mapping
- [API Reference](api/index.md) for exact signatures and public types

## From Book To Deployment

The book develops strategies through research, diagnostics, and backtesting. `ml4t-live` takes the
same strategy surface and runs it against real brokers with explicit deployment controls around it.

If you are reading *Machine Learning for Trading, Third Edition*, the practical sequence is:

1. develop and validate the strategy in the book workflow
2. map the notebook to reusable APIs in the [Book Guide](book-guide/index.md)
3. port the strategy into `LiveEngine`
4. start in shadow mode under `SafeBroker`
5. promote only after paper-trading and operational checks pass

## Part Of The ML4T Library Suite

```text
ml4t-data -> ml4t-engineer -> ml4t-diagnostic -> ml4t-backtest -> ml4t-live
```

The same `Strategy` class sits at the handoff between `ml4t-backtest` and `ml4t-live`.

## Disclaimer

This library reduces operational risk but does not remove it. Start in shadow mode, validate order
flow and reconciliation carefully, and treat live deployment as a staged process rather than a single
switch from backtest to production.
