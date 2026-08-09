# Examples

`shadow_mode_demo.py`, `startup_reconciliation_demo.py`, and `risk_guard_demo.py` are the
credential-free deterministic set. Release qualification copies all three outside the source tree
and runs them against the installed wheel. The other examples require an external service or paper
credentials and never run in an untrusted pull-request context.

`shadow_mode_demo.py` is the fastest path from install to a working run. It uses a synthetic feed plus `SafeBroker(shadow_mode=True)` and `VirtualPortfolio`, needs no credentials or broker session, prints progress every five seconds, and exits after about a minute.

`startup_reconciliation_demo.py` shows the startup reconciliation path without any live infrastructure. It writes a persisted state snapshot that disagrees with the broker's live positions and orders, connects through `SafeBroker`, and prints the reconciliation report.

`risk_guard_demo.py` demonstrates two runtime guards without a real broker: stale-data rejection and daily-loss kill-switch activation. It accepts one fresh-data shadow order, blocks one stale-data order, then shows the kill switch activating after a simulated equity drop.

`okx_funding_paper.py` uses the public OKX REST API to poll BTC, ETH, and SOL perpetual swaps on 1-minute candles. It needs only outbound HTTPS access to `okx.com`, prints funding snapshots and simple long/short bias labels, and exits after about 70 seconds.

`alpaca_paper_equity.py` connects to Alpaca paper trading for SPY, QQQ, and IWM with a tiny moving-average crossover strategy. It requires `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`, uses paper-only endpoints, prints heartbeats every five seconds, and exits after about 95 seconds.

`ib_paper_equity.py` connects to TWS or IB Gateway on `127.0.0.1:7497` and runs a toy momentum strategy on a few large-cap equities. It requires a local IB paper session with API access and market-data subscriptions, prints heartbeats every five seconds, and exits after about 75 seconds.
