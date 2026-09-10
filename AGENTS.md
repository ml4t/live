# ml4t-live

Live and paper execution for strategies that use the lifecycle and order contracts shared with
`ml4t-backtest`. The package coordinates asynchronous market-data feeds and brokers while exposing
synchronous strategy callbacks, explicit execution modes, persistent risk state, and recovery
behavior.

## Public entry points

```python
from ml4t.live import LiveEngine, LiveRiskConfig, SafeBroker
from ml4t.live import AlpacaBroker, IBBroker
```

Start new integrations in shadow or paper mode. `SafeBroker` applies `LiveRiskConfig` before an order
reaches a broker, and `LiveEngine` manages connection, lifecycle dispatch, strategy execution, and
shutdown.

## Source map

| Path | Responsibility |
|---|---|
| `src/ml4t/live/engine.py` | Engine orchestration and runtime transitions |
| `src/ml4t/live/lifecycle.py` | Synchronous callback dispatch on the live runtime |
| `src/ml4t/live/runtime.py` | Strategy intents and execution-policy validation |
| `src/ml4t/live/safety.py` | Execution modes, limits, reconciliation, and virtual portfolios |
| `src/ml4t/live/persistence.py` | Risk state and execution-journal integrity |
| `src/ml4t/live/brokers/` | Interactive Brokers and Alpaca adapters |
| `src/ml4t/live/feeds/` | Broker, exchange, Databento, and aggregate market-data feeds |
| `tests/integration/` | Cross-component behavior without mandatory live credentials |

## Safety and compatibility

- Keep shadow mode as the safe first execution path and require explicit selection of live routing.
- Do not place credentials in code, fixtures, logs, or committed configuration.
- Preserve public symbols used by book notebooks and downstream libraries.
- Treat state migration, startup reconciliation, callback timeouts, provider gaps, and shutdown
  behavior as part of the shipped contract.
- Treat documentation, examples, and deterministic provider-contract tests as release surfaces.

## Quality commands

```bash
uv sync
uv run ruff check --no-fix src tests
uv run ruff format --check src tests
uv run ty check
uv run pytest
uv run mkdocs build --strict
```
