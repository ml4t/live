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
| `src/ml4t/live/temporal.py` | Shared feed/config calendars, session labels and trading-time health |
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

## Guide maintenance

Update this guide when the public runtime surface, provider ownership, safety contract,
documentation path, or quality command changes. Keep provider credentials, current qualification
runs, issue state, and private operating notes in their designated systems rather than this public
guide.

## Quality commands

```bash
uv sync
uv run ruff check --no-fix src tests
uv run ruff format --check src tests
uv run ty check
uv run pytest
uv run mkdocs build --strict
```

## Maintainer-directed change batching

For our own fixes and agent work directed by the maintainer, use a bounded, coherent PR rather
than one PR per small defect or implementation step. One PR may close several related issues
when the changes serve the same user workflow, subsystem repair or compatibility objective and
can be reviewed and reverted together. Keep implementation, regressions, documentation and
follow-up repairs together; use focused commits and retain evidence for each defect. Do not
create separate issues or PRs merely for individual files, tests, assertions or platform variants.

Split unrelated changes, independently risky or breaking changes, separate ownership boundaries,
and work that needs independent release or rollback decisions. A general maintenance label alone
does not make a batch coherent. Reuse or safely consolidate existing owned submissions instead of
opening overlapping PRs; preserve contributor attribution, recoverable history and other owners'
active work. Do not rewrite or close someone else's work merely to reduce the queue count.

Run focused checks while iterating, then qualify the complete final revision with every applicable
required gate. Group related work before expensive CI; do not weaken checks, bypass review or
claim old-head checks qualify changed code. This maintainer-directed batching rule takes precedence
over a literal one-issue-per-PR reading of contributor intake guidance.
