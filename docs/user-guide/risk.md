# Risk Controls

`SafeBroker` is the pre-trade control layer around a live broker. It validates each order before submission, persists risk state across restarts, and keeps the kill switch sticky until you clear it.

## Why Risk Controls Live Outside The Strategy

Backtests often treat risk policy as part of simulation assumptions. Live deployment needs those
controls to be explicit, restart-safe, and broker-adjacent. `SafeBroker` is the layer that enforces
that policy consistently regardless of strategy code.

## What Is Enforced Today

At order submission time, `SafeBroker` currently enforces:

- position and exposure caps
- per-order notional and share limits
- order-rate limiting and duplicate-order suppression
- asset allow/block lists
- price-deviation checks for priced orders
- stale-data rejection via `max_data_staleness_seconds`
- daily-loss rejection via `max_daily_loss`
- drawdown-triggered kill-switch activation via `max_drawdown_pct`
- shadow-mode execution through `VirtualPortfolio`

The market-price checks now use the latest cached market snapshot from the engine instead of a placeholder fallback for first-entry trades.

## LiveRiskConfig

```python
from ml4t.live import LiveRiskConfig

config = LiveRiskConfig(
    max_position_value=25_000,
    max_position_shares=1_000,
    max_total_exposure=100_000,
    max_positions=10,
    max_order_value=5_000,
    max_order_shares=250,
    max_orders_per_minute=5,
    max_daily_loss=2_000,
    max_drawdown_pct=0.10,
    max_price_deviation_pct=0.05,
    max_data_staleness_seconds=60,
    dedup_window_seconds=1.0,
    allowed_assets={"SPY", "QQQ"},
    blocked_assets={"GME"},
    execution_mode="shadow",
    kill_switch_enabled=False,
    fail_on_reconciliation_mismatch=True,
    state_file=".ml4t_risk_state.json",
    journal_file=".ml4t_execution_journal.jsonl",
)
```

Set an individual numeric limit to `None` to disable only that check. Numeric limits reject NaN and
infinity. Position and order share limits accept positive fractional values; position counts and
orders-per-minute remain positive integers.

## Control Groups

### Position Limits

- `max_position_value`
- `max_position_shares`
- `max_total_exposure`
- `max_positions`

### Order Limits

- `max_order_value`
- `max_order_shares`
- `max_orders_per_minute`
- `dedup_window_seconds`

### Loss And Safety Limits

- `max_daily_loss`
- `max_drawdown_pct`
- `max_price_deviation_pct`
- `max_data_staleness_seconds`
- `allowed_assets`
- `blocked_assets`

### Execution And Persistence

- `execution_mode`: required `"shadow"`, `"paper"`, or `"live"` destination
- `kill_switch_enabled`
- `fail_on_reconciliation_mismatch`
- `state_file`
- `journal_file`
- `fail_on_journal_error`

## Persisted State

The risk state file persists more than just the kill switch. It now captures:

- current trading date and daily counters
- daily-loss baseline through `session_start_equity`
- persisted position and pending-order snapshots from the last clean disconnect
- unresolved cancel-and-resubmit replacement gaps
- kill-switch state and reason

That persisted snapshot is used again on the next `SafeBroker.connect()` to generate the startup reconciliation report.
If `fail_on_reconciliation_mismatch=True`, a non-clean reconciliation raises `ReconciliationMismatchError` and blocks startup outside shadow mode.

State is stored in a versioned envelope with a generation number and SHA-256 integrity check.
State, journal, integrity-head, and lock files are created and replaced with mode `0600`. A state
path that is a symlink, is not owned by the current user, has another mode, contains invalid field
types, fails its integrity check, or uses an unsupported schema blocks `SafeBroker` construction.
One `SafeBroker` owns an exclusive writer lease until disconnect or `close_persistence()`.

## Execution Journal

`SafeBroker` also writes a JSONL execution journal. By default it lives next to the risk-state file and includes:

- order submissions and shadow fills
- manual or automatic kill-switch events
- startup reconciliation outcomes
- engine health transitions and recovery attempts when the broker is used through `LiveEngine`

Each journal record includes its sequence, predecessor hash, and record hash. A separate atomic
head file detects removal of complete trailing records. The default `fail_on_journal_error=True`
writes an order-intent record before a broker call and blocks the call if that write fails. Set it
to `False` only when a documented deployment policy permits trading without an audit record;
`safe_broker.persistence_status` reports the active policy and any failure.

Set `journal_file` explicitly when you want the journal stored elsewhere. It must not overlap the
state path or state lock.

## Migration And Recovery

A structurally valid legacy state file is converted to the versioned envelope when `SafeBroker` is
first constructed. Before migration, the legacy file must already be a regular file owned by the
current user with mode `0600`.

Corrupt, truncated, tampered, unsafe, and unsupported files are never replaced during startup.
Keep the original file as diagnostic evidence and restore a known-good backup to a new path. An
existing unchained journal is not rewritten in place; archive it and configure a new journal path.
Do not retry an order after `AcceptedOrderPersistenceError`: the venue or shadow portfolio already
accepted it, and operator reconciliation must determine its state.

## Shadow Mode

Shadow mode is the recommended first deployment step:

```python
safe_broker = SafeBroker(broker, LiveRiskConfig(execution_mode="shadow"))
```

In shadow mode:

- all normal risk checks still run
- orders are marked filled virtually
- `VirtualPortfolio` tracks positions and cash locally
- no real broker order is submitted

## Kill Switch

The kill switch can be activated manually or by a loss breach:

```python
safe_broker.enable_kill_switch("manual halt")
safe_broker.disable_kill_switch()
```

Kill-switch state is persisted in `state_file`, so it survives process restarts until it is manually cleared.

## SafeBroker Usage

```python
from ml4t.live import IBBroker, LiveRiskConfig, SafeBroker

raw_broker = IBBroker(port=7497)
safe_broker = SafeBroker(
    raw_broker,
    LiveRiskConfig(
        execution_mode="shadow",
        max_position_value=10_000,
        max_order_value=2_500,
        max_daily_loss=500,
        max_data_staleness_seconds=60,
    ),
)
```

Inside `Strategy.on_data(...)`, order placement stays synchronous because `LiveEngine` passes a thread-safe broker wrapper to the strategy:

```python
def on_data(self, timestamp, data, context, broker):
    if broker.get_position("AAPL") is None:
        broker.submit_order("AAPL", 10)

    # Pending orders can also be updated through the sync wrapper.
    # broker.replace_order("ML4T-1", limit_price=189.5)
```

## Errors

Risk violations raise `RiskLimitError`:

```python
from ml4t.live import RiskLimitError

try:
    await safe_broker.submit_order_async("AAPL", 10_000)
except RiskLimitError as exc:
    print(f"blocked: {exc}")
```

## Recommended Progression

1. Start in shadow mode.
2. Validate order flow, virtual positions, and runtime health.
3. Move to paper credentials with conservative limits.
4. Check `ml4t-live status` and startup reconciliation before going live.
5. Increase size only after repeated clean starts and expected fills.

For the broader chapter map, use the [Book Guide](../book-guide/index.md).
