# ml4t-live

[![Python 3.12-3.14](https://img.shields.io/badge/python-3.12--3.14-blue.svg)](https://www.python.org/downloads/)
[![PyPI](https://img.shields.io/pypi/v/ml4t-live)](https://pypi.org/project/ml4t-live/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Live trading runtime for ML4T strategies with broker integrations, risk checks, and shadow mode.

Use `ml4t-live` after a strategy has passed research and backtesting. It runs strategies that follow
lifecycle version 1 against Interactive Brokers or Alpaca, applies `SafeBroker` limits, and supports
a staged path from shadow mode to paper trading and live execution.

## Installation

`ml4t-live` supports Python 3.12, 3.13, and 3.14 on Linux, macOS, and Windows.

```bash
uv add ml4t-live
```

The default installation includes the Interactive Brokers, Alpaca, CCXT, and OKX adapters. Install
the `experimental` extra only when evaluating the DataBento feed:

```bash
uv add 'ml4t-live[experimental]'
```

Broker execution requires an account, a configured paper or live environment, and market-data
permissions. The credential-free quick start below verifies the installed public API and shadow
portfolio behavior without contacting a broker or placing an order.

## Quick start

```python
from ml4t.backtest import Order, OrderSide, OrderStatus
from ml4t.live import VirtualPortfolio

portfolio = VirtualPortfolio(initial_cash=100_000)
fill = Order(
    asset="SPY",
    side=OrderSide.BUY,
    quantity=10,
    order_id="shadow-1",
    status=OrderStatus.FILLED,
    filled_price=500.0,
    filled_quantity=10,
)
portfolio.process_fill(fill)

position = portfolio.positions["SPY"]
assert position.quantity == 10
assert portfolio.cash == 95_000
print(f"shadow position: {position.quantity:g} SPY")
```

Expected output:

```text
shadow position: 10 SPY
```

Continue with the [shadow-mode tutorial](https://www.ml4trading.io/docs/live/getting-started/quickstart/)
before connecting a provider.

## Supported boundary

- `LiveEngine`, `SafeBroker`, Interactive Brokers and Alpaca execution adapters, the OKX funding
  feed, and typed bar aggregation are stable-supported within their documented capabilities.
- Alpaca, Interactive Brokers, generic CCXT, and DataBento feeds require explicit experimental
  opt-in with `experimental=True`. Experimental adapters report their missing guarantees on first
  use.
- Shadow mode never routes orders to the wrapped broker. Paper and live modes require explicit
  execution identity and provider-specific qualification.
- State and journal files enforce current-user ownership and mode `0600` on POSIX systems. On
  Windows, restrict the containing directory with service-account ACLs.
- The package has no hardware-specific acceleration or hardware dependency.

Read the [support and qualification boundary](https://www.ml4trading.io/docs/live/qualification/)
before promoting a strategy beyond shadow mode.

## Documentation

- [Installation](https://www.ml4trading.io/docs/live/getting-started/installation/)
- [Backtest-to-live migration](https://www.ml4trading.io/docs/live/user-guide/backtest-to-live/)
- [Risk controls](https://www.ml4trading.io/docs/live/user-guide/risk/)
- [Brokers](https://www.ml4trading.io/docs/live/user-guide/brokers/)
- [Data feeds](https://www.ml4trading.io/docs/live/user-guide/feeds/)
- [Operator CLI](https://www.ml4trading.io/docs/live/user-guide/cli/)
- [API reference](https://www.ml4trading.io/docs/live/api/)

`ml4t-backtest` provides the portable `Strategy` lifecycle used here. `ml4t-specs` defines the
shared lifecycle and canonical intent contracts. See the
[ML4T libraries](https://www.ml4trading.io/libraries/) page for the broader ecosystem.

## Development

```bash
git clone https://github.com/ml4t/live.git
cd live
uv sync --all-extras --dev --locked
uv run python scripts/qualification/run_stable_gate.py
```

The full gate runs lint, formatting, type checking, tests, coverage, stress and performance checks,
strict documentation, package builds, clean artifact installs, and security qualification.

Report defects through the [issue tracker](https://github.com/ml4t/live/issues). Report suspected
vulnerabilities through [private vulnerability reporting](https://github.com/ml4t/live/security/advisories/new),
not a public issue. Release notes and artifacts are available from
[GitHub Releases](https://github.com/ml4t/live/releases).

## License

[MIT](LICENSE)
