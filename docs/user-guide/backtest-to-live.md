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

The maintained chapter material uses both engine dispatch paths and compares lifecycle plus
canonical intent:

- Chapter 25.1 and `code/25_live_trading/01_unified_framework_demo.py`
- Chapter 25.6 and `code/25_live_trading/08_pipeline_verification.py`
- Chapter 25.7 and `code/25_live_trading/10_safety_risk_demo.py`

Use the [Book Guide](../book-guide/index.md) for the broader chapter map.
