# Examples

The examples are meant to answer practical operator and integration questions quickly. They are short, bounded runs, not full applications.

The credential-free examples are deterministic release inputs. Artifact qualification copies them
outside the checkout and runs them with the installed wheel. The remaining examples require a
network service, credentials, or both and run only under explicit external qualification.

## Fastest Start

If you want one example that works without credentials, start here:

```bash
uv run python examples/shadow_mode_demo.py
```

That exercises `LiveEngine`, `SafeBroker`, `VirtualPortfolio`, and shadow-mode order handling with a synthetic feed.

## Example Index

| Example | What it shows | Requirements | Run |
| --- | --- | --- | --- |
| `shadow_mode_demo.py` | shadow mode, synthetic feed, virtual fills | none | `uv run python examples/shadow_mode_demo.py` |
| `okx_funding_paper.py` | public OKX funding feed on 1-minute bars | outbound HTTPS to `okx.com` | `uv run python examples/okx_funding_paper.py` |
| `alpaca_paper_equity.py` | Alpaca paper broker + Alpaca feed + small MA strategy | `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | `uv run python examples/alpaca_paper_equity.py` |
| `ib_paper_equity.py` | IB paper broker + IB feed + small momentum strategy | TWS or IB Gateway on `127.0.0.1:7497` | `uv run python examples/ib_paper_equity.py` |
| `startup_reconciliation_demo.py` | startup reconciliation between persisted and live broker state | none | `uv run python examples/startup_reconciliation_demo.py` |
| `risk_guard_demo.py` | stale-data rejection and daily-loss kill-switch activation | none | `uv run python examples/risk_guard_demo.py` |

## Choosing The Right Example

Use `shadow_mode_demo.py` when you want the lowest-friction end-to-end run.

Use `startup_reconciliation_demo.py` when you are validating the operational startup path and want to see what a mismatch report looks like before connecting to a real broker.

Use `risk_guard_demo.py` when you want to inspect the two most important runtime safety checks added in the hardening pass: stale market data rejection and daily-loss enforcement.

Use the Alpaca and IB examples only after the credential-free demos are behaving the way you expect.
