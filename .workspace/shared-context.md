# Shared Project Context: ml4t-live

## Package

- Package name: `ml4t-live`
- Import path: `ml4t.live`
- Purpose: live trading with a versioned portability contract shared with `ml4t-backtest`

## Core Surface

- `engine.py` - live engine orchestration and lifecycle dispatch
- `lifecycle.py` - versioned synchronous callback dispatch on a dedicated worker
- `wrappers.py` - synchronous strategy access to asynchronous brokers
- `safety.py` - risk controls, shadow mode, and virtual portfolio state
- `brokers/` - Interactive Brokers and Alpaca adapters
- `feeds/` - Alpaca, IB, Databento, CCXT, OKX, and aggregation

## Workflow

```bash
uv sync
uv run ruff check --no-fix src tests
uv run ruff format --check src tests
uv run ty check
uv run pytest
```

## Safety

- Start with `shadow_mode=True`
- Keep public symbols stable for book and notebook consumers
- Treat docs, examples, and tests as part of the shipped library surface
