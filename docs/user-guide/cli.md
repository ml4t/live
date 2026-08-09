# CLI

`ml4t-live` exposes a small operator-facing CLI. It is not a deployment system or a daemon manager. It is a thin front door for two concrete tasks:

- run broker and reconciliation checks with `preflight`
- inspect persisted risk state and reachable broker state with `status`
- run a short shadow session from a strategy file with `shadow`

## Preflight

`preflight` is the operator readiness check. It opens the configured broker, reads account state,
builds a startup reconciliation report, and exits with a non-zero status when the result is
degraded.

```bash
uv run ml4t-live preflight ib --state-file .ml4t_risk_state.json --strict
```

Supported flags:

- `alpaca` or `ib` to select the broker to probe
- `--strict` to fail when startup reconciliation is not clean
- `--require-market-open` to fail when the US equity session is not open
- `--state-file ...` to point preflight at the persisted risk-state file you intend to use

Current output includes:

- broker connectivity and account balances
- kill-switch state carried in the persisted risk file
- whether the current startup reconciliation is clean
- missing or unexpected positions/orders in the reconciliation report
- journal and state-file paths used by the run
- current session state and next session boundary for equities

Use `preflight` before paper or live sessions when you want a fail-fast check instead of a descriptive status dump.

## Status

`status` reads the persisted `SafeBroker` state file and probes brokers when the relevant environment variables are present.

```bash
uv run ml4t-live status --state-file .ml4t_risk_state.json
```

Current output includes:

- persisted risk-state fields such as `orders_placed`, `daily_loss`, and kill-switch state
- persisted position and pending-order snapshots from the last broker disconnect
- the inferred execution journal path and a short tail of recent runtime events when that journal exists
- Alpaca paper connectivity, positions, and open orders when `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` are set
- Interactive Brokers connectivity, positions, and open orders when `IB_HOST` or `IB_PORT` is set

### Broker Probe Environment

```bash
export ALPACA_API_KEY=...
export ALPACA_SECRET_KEY=...
export ALPACA_PAPER=true
export IB_HOST=127.0.0.1
export IB_PORT=7497
export IB_CLIENT_ID=1999
```

`status` skips a broker cleanly when the required environment is missing.
`ALPACA_PAPER=false` tells the CLI to probe a live Alpaca account instead of paper.

## Shadow

`shadow` loads a strategy file, builds a feed, wraps the broker in
`SafeBroker(execution_mode="shadow")`, and runs a bounded live-engine session.

```bash
uv run ml4t-live shadow examples/shadow_mode_demo.py --feed okx --duration 60
```

Supported flags:

- `--feed okx` for the public OKX funding feed
- `--feed alpaca` to explicitly select the experimental Alpaca market-data adapter
- `--duration 60` to stop automatically after a fixed number of seconds
- `--feed-silence-seconds ...` to override the runtime silence threshold used by shadow health reporting
- `--state-file ...` to control where the risk state is persisted

During the run, the CLI prints:

- `health`: `ready`, `ok`, `waiting_for_data`, `feed_silent`, `idle_market_closed`, `broker_disconnected`, `failed`, or `stopped`
- `recovery` and `attempts`: current watchdog recovery state if the engine was configured with bounded auto-recovery
- `session`: `continuous` for non-equity feeds, or the current equity session state for US stocks
- `last_bar_age`: age of the last received bar in wall-clock seconds
- `positions`: current shadow positions from `VirtualPortfolio`
- `recent_intents`: the most recent order intents emitted by the strategy

The default silence threshold is feed-specific:

- `okx`: `90s`, because the public REST-polled minute-bar feed can be bursty
- `alpaca`: `30s`

## What The CLI Is Good For

Use it when you want to answer operator questions quickly:

- would this broker session pass a real startup preflight right now?
- does the persisted state file still show an active kill switch?
- what positions and pending orders were captured at the last clean disconnect?
- what did the most recent runtime journal events say about health transitions or recovery attempts?
- is the feed currently silent because the market is closed, or because data stopped arriving?
- can the strategy file run through `LiveEngine` in shadow mode without broker credentials?

Use the Python API directly when you need a long-running service, custom logging, or orchestration around retries and deployment.
