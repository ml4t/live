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
  `queue_capacity`. Existing calls remain valid. Configure the capacity when overload policy must
  be explicit.
- `BarBuffer.volume` is a float and `update()` accepts fractional volume. Code that serializes or
  validates volume as an integer must accept a finite float.
- Risk limits in `LiveRiskConfig` accept `None` only as an explicit disable value. The beta defaults
  remain numeric. The configuration also adds reducing-risk, persistence-journal, and replacement
  recovery controls.
- `LiveEngine` accepts lifecycle, execution-policy, and maximum-event-age inputs and exposes typed
  runtime state and retained operational transitions.
- The beta risk-state JSON is unversioned. Do not edit or replace it during migration. The stable
  candidate must qualify its pre-connection upgrade or rejection path before release.
- Generic CCXT and DataBento feeds require an explicit experimental opt-in. They are not part of the
  stable feed contract.

## Compatibility After The First Stable Release

`compatibility-policy.toml` defines the enforceable contract. A stable symbol, callable signature,
default, protocol member, dataclass field, enum value, exception base, CLI argument, entry point, or
persisted schema cannot change without an intentional baseline update and migration record.

An incompatible removal requires a `DeprecationWarning` through at least one prior minor release,
this guide must identify the replacement, and removal requires a major release. A security or
financial-safety correction may use the policy's urgent exception only when the release notes state
the incompatibility, explain why the normal interval is unsafe, and provide the safe migration.
