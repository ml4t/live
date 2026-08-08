# Operator Guide

`ml4t-live` is designed for staged rollout. The practical workflow is:

1. shadow mode with bounded runs
2. paper trading with conservative limits
3. small live size with reconciliation and status checks before promotion

## Preflight

Before you route anything real:

- run `uv run ml4t-live preflight ib --state-file .ml4t_risk_state.json --strict`
- confirm preflight exits successfully before you promote a paper or live session
- confirm the persisted risk state does not show an unexpected active kill switch
- check that startup reconciliation is clean, or intentionally explain every mismatch before proceeding
- verify the journal path is where you expect runtime events to land for this session

If you only want a descriptive dump and not a pass/fail check, use `status` instead.

## Shadow Phase

Shadow mode is the correct first deployment step because it exercises the live engine and strategy path without routing a real order.

```bash
uv run ml4t-live shadow examples/shadow_mode_demo.py --feed okx --duration 60
```

What to watch during the run:

- `health=ok` when data is flowing normally
- `health=feed_silent` when the feed is running but no fresh data has arrived within the configured silence window
- `health=idle_market_closed` for US equity feeds outside regular market hours
- `recent_intents` to confirm the strategy is attempting the trades you expect
- `positions` to confirm `VirtualPortfolio` state changes line up with those intents

For the public OKX feed, the shadow CLI uses a looser default silence threshold than streaming feeds. Public REST-polled minute bars can arrive unevenly even when the feed is healthy, so `feed_silent` on OKX should be interpreted relative to that wider threshold unless you override it explicitly.

## Paper Phase

Move to paper trading only after the shadow run looks operationally boring.

That means:

- no unexpected kill-switch activations
- no stale-data rejections unless they are explained by the feed
- no reconciliation surprises on restart
- no uncontrolled order bursts

A paper deployment should still keep tight limits:

```python
LiveRiskConfig(
    shadow_mode=False,
    max_position_value=5_000,
    max_order_value=1_000,
    max_daily_loss=500,
    max_data_staleness_seconds=60,
)
```

If you are running US equities and want preflight to fail outside the regular session window, add `--require-market-open` to the CLI check.

## Reconciliation On Startup

`SafeBroker.connect()` now compares the persisted snapshot from the previous run against the broker's live positions and pending orders.

If the report is clean, the persisted and live snapshots match.

If the report shows missing or unexpected positions/orders, stop and investigate before promoting the session. A mismatch usually means one of these happened:

- a previous run exited uncleanly
- manual broker activity occurred outside `ml4t-live`
- fills or cancellations landed after the last persisted snapshot

If you want the library to fail closed instead of only logging the mismatch, set `fail_on_reconciliation_mismatch=True` in `LiveRiskConfig`.

Use the credential-free example when you want to inspect the shape of the report:

```bash
uv run python examples/startup_reconciliation_demo.py
```

## Live Promotion

Promotion from paper to live should be a size change, not a system change.

Keep the same strategy, broker adapter, and monitoring path. Tighten nothing except your operational discipline:

- run `preflight --strict` before the session starts
- use `status` for the human-readable snapshot and recent journal tail
- check the startup reconciliation report
- start with smaller notional than you think you need
- only scale after repeated clean starts, stable data, and expected fills

## Runtime Health Meanings

The runtime status values are intentionally narrow:

- `ok`: engine running, broker reachable when known, and bars arriving within the silence threshold
- `waiting_for_data`: engine running but no bar has been seen yet
- `feed_silent`: engine running, market session appears open or continuous, but the feed has gone quiet too long
- `idle_market_closed`: no fresh equity data is expected because the market session is closed
- `broker_disconnected`: engine running but the broker connection is known to be down
- `stopped`: engine is not running

Those states are for operator interpretation. They do not replace external process monitoring or deployment supervision.

## Execution Journal

`SafeBroker` now writes a JSONL execution journal next to the risk-state file by default. It records:

- submitted or shadow-filled orders
- kill-switch activations
- startup reconciliation outcomes
- engine health transitions
- watchdog recovery attempts and results

Use `status` when you want a short journal tail, and inspect the JSONL file directly when you need a deeper operator trail.

`status` validates state permissions, schema, integrity, and the journal hash chain before showing
the tail. A persistence error returns a nonzero status and leaves the failing file unchanged. Treat
`AcceptedOrderPersistenceError` as an order that may not be retried: reconcile venue orders and
positions first. State and audit files, including `.lock` and `.head` sidecars, must remain owned by
the service user with mode `0600`.

## Optional Watchdog Recovery

If you want the engine to attempt bounded recovery instead of only reporting degraded health, configure it in Python:

```python
engine = LiveEngine(
    strategy,
    safe_broker,
    feed,
    feed_silence_seconds=60,
    auto_recover=True,
    recovery_cooldown_seconds=5,
    max_recovery_attempts=3,
)
```

This is a best-effort restart of the broker/feed pair, not a substitute for external supervision. If recovery fails repeatedly, the engine stops.
