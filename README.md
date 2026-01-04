# ML4T Live Trading Platform

**Package**: `ml4t-live`
**Status**: ✅ 95% Test Coverage, Production-Ready for Shadow Mode
**Design**: v3.0 (Gemini-reviewed)

Live trading platform enabling copy-paste `Strategy` class from backtesting to live trading with **zero code changes**.

## Features

- ✅ **Zero-Change Strategy Migration**: Copy-paste from backtest to live
- ✅ **Shadow Mode**: Test strategies without placing real orders
- ✅ **Risk Management**: Position limits, order limits, rate limiting
- ✅ **Interactive Brokers Integration**: Full async TWS/Gateway support
- ✅ **Bar Aggregation**: Convert tick data to minute bars
- ✅ **Thread-Safe**: Bridges sync strategies to async brokers
- ✅ **95% Test Coverage**: 208 unit tests + 6 integration tests

## Quick Example

```python
from ml4t.backtest import Strategy, OrderSide
from ml4t.live import LiveEngine, LiveRiskConfig
from ml4t.live.brokers.ib import IBBroker
import asyncio

# Your backtest strategy - unchanged!
class MyStrategy(Strategy):
    def on_data(self, timestamp, data, context, broker):
        if not broker.get_position('SPY'):
            broker.submit_order('SPY', 10, side=OrderSide.BUY)

# Configure for live trading
async def main():
    # Connect to IB (paper trading)
    broker = IBBroker(port=7497)  # Paper trading port
    await broker.connect()

    # Create engine with shadow mode (no real orders!)
    config = LiveRiskConfig(shadow_mode=True, max_position_value=50_000)
    engine = LiveEngine(broker, MyStrategy(), config)

    # Run strategy
    try:
        await engine.run()
    except KeyboardInterrupt:
        print("Stopping...")
    finally:
        await broker.disconnect()

asyncio.run(main())
```

**Output (shadow mode)**:
```
📊 Bar 1: SPY close = $450.02
  → Buying 10 shares of SPY (VIRTUAL - shadow mode)
✅ Virtual position: +10 SPY @ $450.02
🔒 No real orders placed (shadow mode active)
```

## Installation & Quick Start

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager
- IB TWS or Gateway (for live trading)

### Installation

```bash
# Clone and install
pip install ml4t-live

# Or install from source
git clone https://github.com/ml4t/live.git
cd live

# Install dependencies with uv (creates .venv automatically)
uv sync --dev

# Install pre-commit hooks
uv run pre-commit install
```

### Development Workflow

```bash
# Activate virtual environment
source .venv/bin/activate

# Or run commands directly with uv
uv run pytest tests/
uv run mypy src/ml4t/live/
uv run ruff check src/

# Run pre-commit hooks manually
uv run pre-commit run --all-files
```

### Project Structure

```
live/
├── src/ml4t/live/       # Main package
│   ├── brokers/         # Broker implementations (IB, Alpaca, etc.)
│   ├── feeds/           # Data feed implementations
│   ├── protocols.py     # Protocol definitions
│   ├── wrappers.py      # Thread-safe wrappers
│   ├── engine.py        # LiveEngine
│   └── safety.py        # Risk controls, shadow mode
├── tests/               # Unit and integration tests
├── docs/                # Documentation
├── examples/            # Example strategies
├── pyproject.toml       # Project configuration
├── uv.lock             # Locked dependencies
└── .pre-commit-config.yaml  # Pre-commit hooks
```

## Pre-commit Hooks

This project uses pre-commit hooks to ensure code quality:

- **ruff**: Linting and formatting
- **mypy**: Type checking
- **trailing-whitespace**: Remove trailing spaces
- **end-of-file-fixer**: Ensure newline at EOF
- **check-yaml**: Validate YAML files
- **check-added-large-files**: Prevent large file commits
- **check-merge-conflict**: Detect merge conflict markers
- **debug-statements**: Detect debug statements

Hooks run automatically on `git commit`. To run manually:

```bash
uv run pre-commit run --all-files
```

## Dependencies

### Runtime
- `ml4t-backtest` - Strategy, Order, Position types (local editable install)
- `ib_async>=1.0.0` - Async IB API

### Development
- `pytest>=8.0` - Testing framework
- `pytest-asyncio>=0.23` - Async test support
- `pytest-cov>=4.1` - Coverage reporting
- `ruff>=0.1` - Linting and formatting
- `mypy>=1.7` - Type checking
- `pre-commit>=3.0` - Git hooks

## Testing

```bash
# Run all tests
uv run pytest tests/

# Run unit tests only
uv run pytest tests/unit/

# Run with coverage
uv run pytest tests/ --cov=src/ml4t/live --cov-report=term-missing

# Run integration tests (requires IB)
uv run pytest tests/integration/ -v --ib-port=7497
```

## Code Quality

```bash
# Run linter
uv run ruff check src/

# Format code
uv run ruff format src/

# Type checking
uv run mypy src/ml4t/live/
```

## Adding Dependencies

```bash
# Add runtime dependency
uv add package-name

# Add dev dependency
uv add --dev package-name

# Update dependencies
uv sync
```

## Documentation

- **[Quick Start Guide](docs/quickstart.md)** - Get up and running in 5 minutes
- **[IB Setup Guide](docs/ib_setup.md)** - TWS/Gateway configuration and troubleshooting
- **[API Reference](docs/api_reference.md)** - Complete API documentation
- **[DESIGN.md](DESIGN.md)** - Architecture and design decisions

## Safety Disclaimer

**This library is designed for paper trading and educational purposes.**

**Safety Progression:**
1. **Shadow Mode** (1-2 weeks): Verify logic, no real orders
2. **Paper Trading** (2-4 weeks): Test with IB paper account
3. **Live Micro** (1-2 weeks): Tiny positions ($100-500)
4. **Live Small** (ongoing): Gradual size increase

**Risk Management:**
- Always start with `shadow_mode=True`
- Set conservative `max_position_value` and `max_order_value`
- Monitor virtual vs real positions carefully
- Use stop-losses and position limits
- This is NOT a substitute for professional trading systems

## Project Status

**Version**: 0.1.0
**Status**: ✅ Production-Ready for Shadow Mode
**Design**: v3.0 (Gemini reviewed twice)
**Test Coverage**: 95% (208 unit + 6 integration tests)
**Platform Completion**: 87% (20/23 tasks complete)

**Completed:**
- ✅ Core engine with async/sync bridging
- ✅ IB broker integration with TWS/Gateway
- ✅ Shadow mode and risk management
- ✅ Bar aggregation and data feeds
- ✅ 95% test coverage with integration tests
- ✅ Comprehensive documentation

**Remaining:**
- ⏳ Stress testing (memory leaks, long-running)
- ⏳ Example strategies directory

## Contributing

See parent repository for contribution guidelines.

## License

See parent repository for license information.
