# Backtest to Live

`ml4t-backtest` and `ml4t-live` implement lifecycle version 1 from `ml4t-specs`. A strategy is
portable when its callback trace, information boundary, canonical intent, execution policy, and
position-rule state satisfy that contract in both runtimes.

## What Stays The Same

- the `Strategy` subclass when it uses only portable callbacks and broker operations
- `on_start`, causal `on_prepare`, `on_data`, and `on_end` callback semantics
- `CanonicalTargetIntent` decisions and versioned position-rule definitions

## What Changes

| Backtest concept | Live equivalent |
| --- | --- |
| `Engine` | `LiveEngine` |
| historical data feed | broker-connected or replay `DataFeedProtocol` |
| simulated broker/execution | async broker wrapped by `SafeBroker` |
| backtest risk assumptions | explicit `LiveRiskConfig` limits |
| recorded execution policy | venue capabilities and live execution policy |
| offline validation | shadow mode, paper trading, staged live rollout |

## Migration Pattern

1. Validate the strategy in `ml4t-backtest` against lifecycle version 1.
2. Compare callback and canonical-intent traces on one causal tape.
3. Pick the broker and feed combination that matches the required capabilities.
4. Wrap the broker in `SafeBroker` and start with `execution_mode="shadow"`.
5. Promote to paper trading only after signal, intent, inventory, and order-flow checks pass.
6. Go live only after separate operational qualification with conservative limits.

## Minimal Port

```python
from ml4t.live import LiveEngine, LiveRiskConfig, SafeBroker

safe_broker = SafeBroker(raw_broker, LiveRiskConfig(execution_mode="shadow"))
engine = LiveEngine(strategy, safe_broker, live_feed)
await engine.connect()
await engine.run()
```

The strategy class may remain the same. Verify callback traces and canonical intents on a shared
causal tape. Equal signals alone are insufficient because two runtimes can observe different phases,
construct different orders, or apply different position rules after producing the same signal label.

## Shared calendar declarations

The current source targets Backtest `0.2.0` and Specs `0.2.0`; lifecycle version remains `1`.
Published Live `0.1.3` requires the older `0.1.x` cohort and cannot be installed with these `0.2.0`
packages. Do not override its dependency resolver to combine them. The runtime integration in this
source requires its own qualified release.

Pass the same `FeedSpec` to the historical feed and the Live engine:

```python
from ml4t.backtest import BacktestConfig, DataFeed, Engine
from ml4t.backtest.config import DataFrequency
from ml4t.specs import FeedSpec, TimestampSemantics

spec = FeedSpec(
    time_contract_version=2,
    calendar="CME_Equity",
    timezone="UTC",
    data_frequency=DataFrequency.HOURLY,
    timestamp_semantics=TimestampSemantics.EVENT_TIME,
)
config = BacktestConfig(enforce_sessions=True)
historical_feed = DataFeed(prices_df=prices, feed_spec=spec)
backtest = Engine(historical_feed, historical_strategy, config)
live = LiveEngine(
    live_strategy, safe_broker, live_feed, strategy_config=config, feed_spec=spec
)
```

Calendar selection uses an explicit `LiveEngine(calendar=...)` override, then the shared config/feed
resolution, then NYSE. An omitted declaration inherits; explicit `calendar=None` disables exchange
session handling and keeps row-based operation. `calendar="24/7"` names a continuous calendar and
retains calendar session labels. These are distinct declarations in temporal contract version 2.
The engine passes a resolved copy to `on_prepare` without mutating the caller's config or metadata.

Session health and scheduled targets use authoritative exchange sessions, including holidays,
early closes, lunch breaks and overnight session labels. A Sunday CME event can belong to Monday's
session before UTC midnight. `enforce_sessions=True` filters strategy events using the same calendar
interval convention as Backtest: event-time/left labels include the open and exclude the close;
bar-close/right labels exclude the open and include the close. Session-label timestamps select
session dates rather than intraday instants. The default remains `enforce_sessions=False`.

Provider events must still carry aware UTC timestamps. For explicit status-clock arguments, naive
timestamps use the declared feed timezone; ambiguous or nonexistent DST times require an aware
argument. Unknown calendars fail during construction, before broker connection or feed startup.

The installed comparison below executes the causal strategy, CME overnight target, early-close
filter and calendar-health regressions with the actual installed Backtest/Specs versions. Provider
order and extended-session qualification remain separate from this offline comparison. This
three-library workflow does not establish coinstallation of all seven libraries or venue fill parity.

## Portability And Outcome Parity

Lifecycle portability means both engines invoke the same versioned callbacks with the information
allowed in each phase. Canonical-intent parity means `decision_time`, `information_cutoff`, effective
session and phase, target units, rounding, residual policy, reason, and position-rule policy match.

Outcome parity is narrower. Compare outcomes only when execution policy, fees, spread, slippage,
liquidity, bar-path policy, venue capabilities, and starting positions are also equivalent. Live
safety may reduce or reject a portable intent. It must not silently change its meaning.

## Where Divergence Comes From

Technical divergence can arise from:

- different bar completion or event timing
- stale or mismatched data fields
- broker inventory that differs from the simulated portfolio
- order semantics or venue capabilities that differ from the execution policy
- safety rules applied in one runtime but not represented in the comparison
- different lifecycle, dependency, or position-rule versions

`ml4t-live` separates portable decisions from feed, execution, safety, and operator state. A parity
failure can therefore identify the first boundary whose trace differs.

## Practical Rollout Pattern

| Stage | Goal | Required checks |
| --- | --- | --- |
| Shadow mode | Verify the live runtime without venue orders | callbacks, intents, positions, restart behavior |
| Paper trading | Verify broker and feed integration | capabilities, order lifecycle, reconciliation, timestamps |
| Small live size | Verify operational controls | slippage, latency, monitoring, manual overrides |

## Related Pages

- [Lifecycle Migration](migration.md)
- [Quickstart](../getting-started/quickstart.md)
- [Brokers](brokers.md)
- [Data Feeds](feeds.md)
- [Risk Controls](risk.md)
- [API Reference](../api/index.md)

## See It In The Book

The [unified framework notebook](https://github.com/stefan-jansen/machine-learning-for-trading/blob/d2edec54b1c7a6a9d7a97d8129eb05db4491e1eb/25_live_trading/01_unified_framework_demo.ipynb)
calls both Live and Backtest to compare lifecycle and intent traces. The
[pipeline verification notebook](https://github.com/stefan-jansen/machine-learning-for-trading/blob/d2edec54b1c7a6a9d7a97d8129eb05db4491e1eb/25_live_trading/08_pipeline_verification.ipynb)
calls Live to check the research-to-deployment handoff. The
[crypto funding backtest](https://github.com/stefan-jansen/machine-learning-for-trading/blob/d2edec54b1c7a6a9d7a97d8129eb05db4491e1eb/case_studies/crypto_perps_funding/13_backtest.ipynb)
is a related manual case study; its simulation is not a Live run. See the
[Book Guide](../book-guide/index.md) for input and venue differences.

## Verify the first port

Run the [synthetic shadow quickstart](../getting-started/quickstart.md) to check callback dispatch,
virtual positions, and orderly shutdown without a venue. Then compare the same strategy's lifecycle
and canonical intents in Backtest and Live on equivalent input. Record data cutoffs, execution
policy, starting positions, fees, and venue capabilities before interpreting outcome differences.
The [API reference](../api/index.md#engine) gives the Live engine signature.

## Run the installed-candidate comparison

From a Linux checkout of this repository with `uv` installed, run:

```bash
uv run --locked python scripts/qualification/check_dependency_matrix.py
```

The verifier builds the checkout's Live wheel, installs it into separate minimum, locked, and
maximum dependency environments, and runs the shared strategy through the real `Engine` and
`LiveEngine` contract tests. It also checks canonical order and broker contracts. The output names
the installed package versions for each profile; a failed contract or installation returns a
nonzero exit status. No provider credentials or venue orders are required.

The published shadow quickstart stack checked on 2026-10-17 was:

| Package | Version |
| --- | --- |
| `ml4t-live` | `0.1.2` |
| `ml4t-backtest` | `0.1.12` |
| `ml4t-specs` | `0.1.5` |

The complete shadow quickstart passed on that stack. The command above builds the current
checkout instead; use its reported versions when recording a new parity or migration result.
Neither result establishes equal venue execution or qualifies a later dependency release.
