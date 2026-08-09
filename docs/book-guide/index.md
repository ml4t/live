# Book Guide

This guide maps `ml4t-live` to **Machine Learning for Trading, Third Edition** so you can move between
the library docs and the book materials without guessing which notebook or chapter matters.

## How To Use This Guide

- Start with the chapter map if you are reading the book.
- Start with the API map if you are coming from the library and want the matching notebook.
- Treat the listed code paths as the canonical book-side references in the `third_edition` materials.

## Chapter To Feature Map

| Book material | What it teaches | `ml4t-live` connection |
| --- | --- | --- |
| Chapter 16 strategy simulation | event-driven strategies and parity-friendly design | the strategy surface you carry into `LiveEngine` |
| Chapter 18 costs | execution frictions and turnover budgets | the live costs you compare against post-deployment |
| Chapter 19 risk management | kill switches, drawdowns, limits | `LiveRiskConfig`, `SafeBroker`, staged rollout |
| Chapter 25 live trading systems | brokers, feeds, operational parity, deployment | the core `ml4t-live` library surface |
| Chapter 26 MLOps governance | shadow mode, challenger rollout, circuit breakers | operational procedures around `ml4t-live` |

## Notebook And Script Map

| Book path | Why it matters here |
| --- | --- |
| `code/16_strategy_simulation/06_framework_parity.py` | shows why keeping one strategy interface matters before live deployment |
| `code/25_live_trading/unified_framework_demo.py` | demonstrates the same strategy moving from backtest to live-style execution |
| `code/25_live_trading/ib_paper_trading_demo.py` | Interactive Brokers connectivity path |
| `code/25_live_trading/alpaca_paper_trading_demo.py` | Alpaca paper-trading path |
| `code/25_live_trading/alpaca_crypto_live_demo.py` | Alpaca crypto workflow |
| `code/25_live_trading/pipeline_verification.py` | parity checks between research and live workflows |
| `code/25_live_trading/okx_funding_rate_demo.py` | live-style funding-rate deployment with exchange data |
| `code/25_live_trading/safety_risk_demo.py` | `SafeBroker` limits, shadow mode, and kill-switch behavior |
| `code/26_mlops_governance/03_safe_model_rollout.py` | shadow-mode and staged-promotion procedures around live deployment |
| `code/26_mlops_governance/04_circuit_breakers.py` | broader operational safety concepts that complement `SafeBroker` |

## Case Study Link: Crypto Perpetuals Funding

The clearest live-trading case-study bridge in the current book materials is the crypto perpetuals
workflow:

| Case-study path | Library relevance |
| --- | --- |
| `code/case_studies/crypto_perps_funding/03_financial_features.py` | feature definitions that must stay consistent in live inference |
| `code/case_studies/crypto_perps_funding/14_backtest.py` | the validated backtest side of the strategy |
| `code/case_studies/crypto_perps_funding/17_risk_management.py` | portfolio and risk assumptions before deployment |
| `code/25_live_trading/okx_funding_rate_demo.py` | the live-style deployment bridge using exchange funding data |

## From Book Concepts To Library APIs

| Book concept | Library API |
| --- | --- |
| same strategy in backtest and live | `LiveEngine` plus unchanged `Strategy` subclass |
| sync strategy calling async infrastructure | `ThreadSafeBrokerWrapper` |
| explicit deployment risk policy | `LiveRiskConfig` |
| pre-trade enforcement and kill switch | `SafeBroker` |
| paper-like live validation without routing orders | `shadow_mode=True` with `VirtualPortfolio` |
| broker-specific execution path | `IBBroker` or `AlpacaBroker` |
| beta-supported live data source | `IBDataFeed`, `AlpacaDataFeed`, `OKXFundingFeed` |
| experimental opt-in data source | `DataBentoFeed`, `CryptoFeed` |

## What The Book Often Shows Manually

The notebooks are pedagogical and frequently expose mechanics directly. The library turns those same
ideas into reusable interfaces:

- notebook orchestration becomes `LiveEngine`
- ad hoc risk checks become `SafeBroker`
- replay/live feed adapters become `DataFeedProtocol` implementations
- deployment-stage bookkeeping becomes `RiskState` and `VirtualPortfolio`

## Best Reading Path

If you are learning the stack end to end, the most efficient route is:

1. `code/16_strategy_simulation/06_framework_parity.py`
2. [Backtest to Live](../user-guide/backtest-to-live.md)
3. `code/25_live_trading/unified_framework_demo.py`
4. the broker page that matches your venue
5. `code/25_live_trading/safety_risk_demo.py`
6. [Risk Controls](../user-guide/risk.md)

## Related Docs

- [Home](../index.md)
- [Quickstart](../getting-started/quickstart.md)
- [Backtest to Live](../user-guide/backtest-to-live.md)
- [API Reference](../api/index.md)
