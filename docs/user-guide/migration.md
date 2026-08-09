# Migrate A Strategy To Lifecycle Version 1

Use this guide when a prerelease strategy relies on historical initialization, internal risk
callbacks, or implicit opening execution. Complete the migration in backtest before connecting a
live broker.

## Replace Removed Callbacks

Lifecycle version 1 supports `on_start`, causal initialization through `on_prepare`, market events
through `on_data`, and `on_end`. The engines reject these historical surfaces before broker or
strategy side effects:

| Removed surface | Failure | Supported alternative |
| --- | --- | --- |
| `on_before_risk` | `HistoricalStrategyCompatibilityError` for `pre_open` | register an explicit target and position-rule policy during a causal phase |
| `on_historical_data` | `HistoricalStrategyCompatibilityError` for causal initialization | load prior completed data before engine construction or use bounded `on_prepare` inputs |
| `on_prepare(..., timestamps)` | `HistoricalStrategyCompatibilityError` for causal initialization | use `on_prepare(broker, config=None)` without a future session calendar |

Do not catch these errors and continue. Both engines validate the strategy before creating trading
side effects, so a failed migration leaves broker, persistence, and strategy state unchanged.

## Make Initialization Causal

`on_prepare` may use configuration, restored portable state, and prior completed data. It may not
inspect the current session's open, high, low, close, or later timestamps. Precompute model
artifacts outside the engine and pass only the artifact identity and prior information into the
strategy.

## Register Opening Targets Explicitly

An opening target is a `CanonicalTargetIntent` whose effective phase is `pre_open`. Set all of these
fields explicitly:

- `decision_time` in UTC
- `information_cutoff` no later than the decision time
- the effective session and `pre_open` phase
- target measure and signed values
- rounding and residual policies
- an idempotency key and typed reason
- a position-rule policy identifier when rules apply

The decision time and information cutoff must precede the venue opening cutoff. A target registered
after the opening event is rejected. Client opening execution uses the official open supplied by the
opening event. Broker-native opening execution requires the venue's declared opening-auction
capability and submission before that event.

## Record Execution And Position Policies

Outcome comparisons require the same versioned execution policy. Record the market fill phase,
fees, spread, slippage, impact, latency, liquidity fraction, partial-fill rule, order behaviors, and
bar-path policy. The default ambiguous-bar behavior is `reject_ambiguous`; do not select an OHLC
path after observing the bar.

Attach position-rule definitions to the canonical target by policy identifier. Stop-loss,
take-profit, trailing, time-exit, scaled-exit, and composed rules retain activation, entry,
water-mark, remaining-quantity, action, and exit-reason state. Recovery restores this state before
another intent can be accepted.

## Verify The Port

Run both real engine dispatch paths on the same completed-event tape. Compare:

1. lifecycle callback names, phases, and event times
2. complete serialized `CanonicalTargetIntent` values
3. child-order intents under the same execution policy
4. position-rule state transitions
5. final fills only when execution assumptions and starting state also match

Signal equality alone does not pass this check. A missing callback, different information cutoff,
or different target policy is a migration failure even when both strategies emit `BUY`.

See [Backtest to Live](backtest-to-live.md) for the portability boundary and
[Candidate Qualification](../qualification.md) for the complete package gate.

## Migrate From The Published Beta

The published `0.1.0b3` package remains the prerelease API migration baseline. The first stable
candidate removes no root export from that release, but the following changes require review:

- Custom broker implementations must satisfy the complete async broker protocol, including
  connection state, positions, pending orders, cancellation, replacement, and paper-identity
  checks. Strategy-facing brokers also expose canonical target and position-rule operations.
- `AlpacaDataFeed`, `IBDataFeed`, `BarAggregator`, and `OKXFundingFeed` accept an optional bounded
  `queue_capacity`. Configure the capacity when overload policy must be explicit. Alpaca and IB
  feed construction now also requires `experimental=True`; their public imports remain available.
- `BarBuffer.volume` is a float and `update()` accepts fractional volume. Code that serializes or
  validates volume as an integer must accept a finite float.
- Risk limits in `LiveRiskConfig` accept `None` only as an explicit disable value. The beta defaults
  remain numeric. The configuration also adds reducing-risk, persistence-journal, and replacement
  recovery controls.
- `LiveEngine` accepts lifecycle, execution-policy, and maximum-event-age inputs and exposes typed
  runtime state and retained operational transitions.
- Synchronous lifecycle callbacks have a five-second default deadline. An over-deadline callback
  is allowed to finish so it cannot continue mutating state during cleanup, then
  `StrategyCallbackTimeoutError` aborts the run. Set `strategy_callback_timeout_seconds` to an
  explicit positive duration when a reviewed callback legitimately needs longer.
- Remove the beta `halt_on_error` argument. Lifecycle version 1 always stops, finalizes, and
  reraises a strategy exception; the beta flag was ignored and could imply unsupported recovery.
- `LiveRiskConfig` requires an explicit `execution_mode`. The old `shadow_mode=True` form remains a
  compatibility alias. Replace `shadow_mode=False` with either `execution_mode="paper"` or
  `execution_mode="live"`; the library cannot infer the intended account safely.
- `LiveEngine.strategy_config` is a stable backtest configuration passed to `on_prepare`. Code that
  reads the instance attribute should treat it as configuration owned by the engine.
- Crossed quote events now include `crossed` and `crossed_policy` metadata. Consumers that validate
  exact feed metadata objects must accept these documented keys.
- Alpaca, IB, generic CCXT, and DataBento feeds require an explicit experimental opt-in. Alpaca
  bars and quotes lack complete provider sequences, while IB pending-ticker snapshots can lack both
  provider event time and sequence. These adapters are not part of the stable feed contract.

### Upgrade Persisted Beta State

Keep the state file and its journal files together. Construct `SafeBroker` with the same state and
journal paths and the intended execution mode. Construction validates the complete state and the
journal chain before replacing any file or connecting to a provider.

The supported upgrade paths are:

- The unversioned `0.1.0b3` risk-state object is written as the checksummed version-1 envelope.
  Daily loss, order count, equity watermarks, positions, pending orders, and kill-switch state are
  retained. The selected execution mode is added during this one-time replacement.
- A qualified `0.1.0b4` envelope retains the audit journal and upgrades its unversioned portable
  strategy state to schema version 1. Target and reconciliation records are validated. Child
  decision and effective sessions are derived from their target. Position-rule identifiers and
  entry side are restored from the policy and persisted position, while watermarks, exit reason,
  duration, context, filled quantities, and idempotency records remain unchanged.

An invalid journal, corrupt field, ambiguous legacy position side, or unsupported future schema
raises `AuditJournalError` or `CorruptStateError`. The state and journal remain byte-for-byte
unchanged, and no broker method or strategy callback runs. Do not retry after editing the state by
hand. Restore the original files and resolve the reported incompatibility first.

## Compatibility After The First Stable Release

`compatibility-policy.toml` defines the enforceable contract. A stable symbol, callable signature,
default, protocol member, dataclass field, enum value, exception base, CLI argument, entry point, or
persisted schema cannot change without an intentional baseline update and migration record.

An incompatible removal requires a `DeprecationWarning` through at least one prior minor release,
this guide must identify the replacement, and removal requires a major release. A security or
financial-safety correction may use the policy's urgent exception only when the release notes state
the incompatibility, explain why the normal interval is unsafe, and provide the safe migration.

## Select An Execution Destination Explicitly

The stable candidate rejects `SafeBroker` construction unless `LiveRiskConfig.execution_mode` is
set to `"shadow"`, `"paper"`, or `"live"`. The old `shadow_mode=False` default could route an order
to whichever account a provider happened to expose, so preserving that behavior would create a
financial-safety risk.

Replace `LiveRiskConfig(shadow_mode=True)` with `LiveRiskConfig(execution_mode="shadow")`. Replace
`LiveRiskConfig(shadow_mode=False)` with `execution_mode="paper"` or `execution_mode="live"` after
choosing the intended destination. `shadow_mode=True` remains a prerelease compatibility alias for
shadow execution, but new code should use `execution_mode`.

Paper and live execution also require the connected broker identity to match the selected mode.
Alpaca checks the official endpoint and client sandbox setting. IB checks the standard paper or live
port, the managed-account form, and, for live execution, an explicitly configured `IB_ACCOUNT` or
`ML4T_IB_ACCOUNT`. A mismatch fails before persistence or an order API call.

Risk-state files record their execution mode. Use a separate state-file path for each destination.
Reusing a shadow or paper state file for another destination is rejected before the broker connects.
