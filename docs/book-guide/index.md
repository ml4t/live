# Book Guide

The public companion files for *Machine Learning for Trading, Third Edition* show the methods and
operating decisions behind Live workflows. These links point to commit
[`d2edec54b1c7a6a9d7a97d8129eb05db4491e1eb`](https://github.com/stefan-jansen/machine-learning-for-trading/tree/d2edec54b1c7a6a9d7a97d8129eb05db4491e1eb).
Each file below was checked in that Git tree. "Calls Live" means its code imports `ml4t.live`;
"manual" means it teaches a method without that import; "related" means another workflow informs
Live decisions. A matching topic alone does not mean a notebook runs this library.

The Alpaca and IB notebooks use experimental feeds. Their broker adapters have a separate stable
paper contract.

## Run and control a Live strategy

- [Unified framework demo][unified] calls Live and Backtest to compare lifecycle traces and
  portable strategy decisions. Follow [Backtest to Live](../user-guide/backtest-to-live.md).
- [IB paper trading demo][ib-paper] calls Live with an IB paper broker and experimental IB feed. It
  requires TWS or IB Gateway. Follow [Brokers](../user-guide/brokers.md).
- [Alpaca paper trading demo][alpaca-paper] calls Live with Alpaca paper execution and an
  experimental Alpaca feed. It requires credentials. Follow [Brokers](../user-guide/brokers.md).
- [Alpaca crypto live demo][alpaca-crypto] calls Live for a crypto venue workflow. Review its live
  order prerequisites before attempting it. Follow [Brokers](../user-guide/brokers.md).
- [Pipeline verification][pipeline] calls Live to compare research and deployment inputs and
  decisions. Follow [Backtest to Live](../user-guide/backtest-to-live.md).
- [Safety and risk demo][safety] calls Live to exercise `SafeBroker` and shadow risk behavior.
  Follow [Risk Controls](../user-guide/risk.md).
- [Runtime safety showcase][runtime-safety] calls Live to show stale-data rejection, persistent
  kill switch, reconciliation, and engine health. Follow the [Operator Guide](../user-guide/operator-guide.md).
- [Crypto funding deployment loop][funding-loop] calls `AlpacaBroker` only in its
  operator-authorized paper path. It uses OKX public data for a separate execution rehearsal.
  Follow [Data Feeds](../user-guide/feeds.md).

The crypto funding loop trains on Binance-derived perpetual data, observes OKX funding and bars,
and maps a subset to Alpaca USD spot crypto for paper execution. The venues and instruments differ;
its paper orders do not reproduce a perpetual-futures backtest. The notebook needs its case-study
artifacts and outbound data access, and paper submission needs credentials and explicit operator
opt-in. The [synthetic shadow quickstart](../getting-started/quickstart.md) is the local first run.

## Learn the methods before deployment

- [Framework parity][parity] is a related Backtest workflow that compares strategy behavior before
  a Live port. It does not import Live. Follow [Backtest to Live](../user-guide/backtest-to-live.md).
- [Safe model rollout][rollout] teaches staged promotion and shadow evaluation manually. It does
  not import Live. Follow the [Operator Guide](../user-guide/operator-guide.md).
- [Circuit breakers][circuit] teaches operational safety manually. It does not implement
  `SafeBroker`. Follow [Risk Controls](../user-guide/risk.md).
- [Crypto financial features][features] is a related feature workflow using Engineer. It does not
  import Live. Follow [Backtest to Live](../user-guide/backtest-to-live.md).
- [Crypto backtest][crypto-backtest] simulates funding-aligned decisions manually. It does not
  import Live. Follow [Backtest to Live](../user-guide/backtest-to-live.md).
- [Crypto risk management][crypto-risk] studies sizing and risk manually. It does not import Live.
  Follow [Risk Controls](../user-guide/risk.md).

The case-study files use research data, model artifacts, and their own simulation assumptions.
`LiveEngine` needs a feed, venue capability check, broker account state, and explicit `LiveRiskConfig`.
Do not treat a research result as a qualified paper or live run.

## Find the public interface

- Run lifecycle callbacks with `LiveEngine` and `ThreadSafeBrokerWrapper`:
  [Backtest to Live](../user-guide/backtest-to-live.md).
- Limit and audit orders with `LiveRiskConfig`, `SafeBroker`, `RiskState`, and `VirtualPortfolio`:
  [Risk Controls](../user-guide/risk.md).
- Connect `IBBroker` or `AlpacaBroker`: [Brokers](../user-guide/brokers.md).
- Choose `OKXFundingFeed`, `BarAggregator`, or an experimental provider feed:
  [Data Feeds](../user-guide/feeds.md).
- Inspect or restart with `LiveEngine.runtime_status` and `SafeBroker.reconciliation_report`:
  [Operator Guide](../user-guide/operator-guide.md).

See the [API Reference](../api/index.md) for exact signatures and options.

[unified]: https://github.com/stefan-jansen/machine-learning-for-trading/blob/d2edec54b1c7a6a9d7a97d8129eb05db4491e1eb/25_live_trading/01_unified_framework_demo.ipynb
[ib-paper]: https://github.com/stefan-jansen/machine-learning-for-trading/blob/d2edec54b1c7a6a9d7a97d8129eb05db4491e1eb/25_live_trading/03_ib_paper_trading_demo.ipynb
[alpaca-paper]: https://github.com/stefan-jansen/machine-learning-for-trading/blob/d2edec54b1c7a6a9d7a97d8129eb05db4491e1eb/25_live_trading/04_alpaca_paper_trading_demo.ipynb
[alpaca-crypto]: https://github.com/stefan-jansen/machine-learning-for-trading/blob/d2edec54b1c7a6a9d7a97d8129eb05db4491e1eb/25_live_trading/05_alpaca_crypto_live_demo.ipynb
[pipeline]: https://github.com/stefan-jansen/machine-learning-for-trading/blob/d2edec54b1c7a6a9d7a97d8129eb05db4491e1eb/25_live_trading/08_pipeline_verification.ipynb
[safety]: https://github.com/stefan-jansen/machine-learning-for-trading/blob/d2edec54b1c7a6a9d7a97d8129eb05db4491e1eb/25_live_trading/10_safety_risk_demo.ipynb
[runtime-safety]: https://github.com/stefan-jansen/machine-learning-for-trading/blob/d2edec54b1c7a6a9d7a97d8129eb05db4491e1eb/25_live_trading/13_runtime_safety_showcase.ipynb
[funding-loop]: https://github.com/stefan-jansen/machine-learning-for-trading/blob/d2edec54b1c7a6a9d7a97d8129eb05db4491e1eb/25_live_trading/09_crypto_funding_deployment_loop.ipynb
[parity]: https://github.com/stefan-jansen/machine-learning-for-trading/blob/d2edec54b1c7a6a9d7a97d8129eb05db4491e1eb/16_strategy_simulation/06_framework_parity.ipynb
[rollout]: https://github.com/stefan-jansen/machine-learning-for-trading/blob/d2edec54b1c7a6a9d7a97d8129eb05db4491e1eb/26_mlops_governance/03_safe_model_rollout.ipynb
[circuit]: https://github.com/stefan-jansen/machine-learning-for-trading/blob/d2edec54b1c7a6a9d7a97d8129eb05db4491e1eb/26_mlops_governance/04_circuit_breakers.ipynb
[features]: https://github.com/stefan-jansen/machine-learning-for-trading/blob/d2edec54b1c7a6a9d7a97d8129eb05db4491e1eb/case_studies/crypto_perps_funding/03_financial_features.ipynb
[crypto-backtest]: https://github.com/stefan-jansen/machine-learning-for-trading/blob/d2edec54b1c7a6a9d7a97d8129eb05db4491e1eb/case_studies/crypto_perps_funding/13_backtest.ipynb
[crypto-risk]: https://github.com/stefan-jansen/machine-learning-for-trading/blob/d2edec54b1c7a6a9d7a97d8129eb05db4491e1eb/case_studies/crypto_perps_funding/15_risk_management.ipynb
