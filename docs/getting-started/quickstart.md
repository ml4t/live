# Quickstart: complete a shadow run

Run a strategy through `LiveEngine` and `SafeBroker` with synthetic bars. The example needs Python
3.12-3.14 and `uv`; it needs no credentials, market-data subscription, or network service after
installation. It never sends an order to a venue.

## Run the example

Get the maintained example from the public repository, then run it with the published package:

```bash
git clone https://github.com/ml4t/live.git
cd live
uv run --no-project --with ml4t-live python examples/shadow_mode_demo.py
```

The script defines a synthetic feed that emits one `DEMO` bar per second for 65 seconds. A moving
average strategy submits a buy or sell when its signal changes. `SafeBroker` records virtual fills
and positions under `execution_mode="shadow"`; the example broker raises if it receives a real
order. The input and full program are in
[`examples/shadow_mode_demo.py`](https://github.com/ml4t/live/blob/main/examples/shadow_mode_demo.py).

The run prints `Starting shadow mode demo with a synthetic feed.`, timestamped prices, five-second
position heartbeats, and `Finished shadow mode demo. final_positions=...`. The final position may
be flat or long because the synthetic signal changes during the run. A completed final line means
the engine stopped cleanly. The printed positions show what the virtual portfolio held; no venue
state changed.

To check the installed API without waiting for the full run, the [README quick start](https://github.com/ml4t/live/blob/main/README.md)
processes a single virtual fill and asserts its resulting position and cash balance. For the next
operator task, the [reconciliation example](../user-guide/examples.md) shows a restart mismatch
without a broker connection.

## Understand the boundary

This first run proves that the strategy callback, engine lifecycle, safety wrapper, and virtual
portfolio work with synthetic input. It does not qualify a broker, external feed, or restart on an
active account. A real paper session needs broker credentials or TWS/IB Gateway, the applicable
market-data access, a compatible feed, explicit `execution_mode="paper"`, and clean startup
reconciliation. Alpaca and IB feeds require `experimental=True` and do not have stable reconnect
continuity guarantees.

The public OKX funding feed needs outbound HTTPS but no credentials. To try it with the CLI, see
[Run a bounded shadow session](../user-guide/cli.md#shadow). A quiet feed or a run with no trade
intent is not evidence that a provider workflow is ready for paper orders.

## Continue

- [Backtest to Live](../user-guide/backtest-to-live.md) explains what stays portable.
- [Risk Controls](../user-guide/risk.md) covers limits, persistence, and the kill switch.
- [Brokers](../user-guide/brokers.md) and [Data Feeds](../user-guide/feeds.md) state provider requirements.
- [API Reference](../api/index.md) gives the released signatures.
- [Book Guide](../book-guide/index.md) connects the tasks to checked companion files.
