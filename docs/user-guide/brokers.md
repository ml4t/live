# Brokers

`ml4t-live` currently ships two broker adapters:

- `IBBroker` for Interactive Brokers
- `AlpacaBroker` for Alpaca stocks and crypto

Both are beta-supported for the documented paper workflows and adapter contract. Support does not
mean that every order type or account feature is portable. A requested behavior must appear in the
adapter's declared execution capabilities before the runtime can use it.

## Choosing A Broker

| Broker | Best for | Notes |
| --- | --- | --- |
| `IBBroker` | global multi-asset workflows, professional routing, established IBKR accounts | requires TWS or IB Gateway and operational familiarity |
| `AlpacaBroker` | fast setup for US equities, ETFs, and Alpaca-supported crypto | strong paper-trading path and simpler API surface |

## Broker Model

The raw broker implementations are asynchronous and satisfy `AsyncBrokerProtocol`. In normal usage they sit behind `SafeBroker`, and strategies interact with a synchronous broker wrapper created by `LiveEngine`.

- Strategy code uses synchronous calls such as `broker.get_position(...)` and `broker.submit_order(...)`
- Infrastructure code uses async methods such as `await broker.get_cash_async()`

## Interactive Brokers

```python
from ml4t.live import IBBroker

broker = IBBroker(
    host="127.0.0.1",
    port=7497,  # paper TWS
    client_id=1,
    account=None,
)

await broker.connect()
```

### Notes

- `7497` is the usual TWS paper port
- `7496` is the usual TWS live port
- `4002` and `4001` are the usual paper/live IB Gateway ports
- TWS or IB Gateway must be running with API access enabled before you connect

## Alpaca

```python
from ml4t.live import AlpacaBroker

broker = AlpacaBroker(
    api_key="YOUR_API_KEY",
    secret_key="YOUR_SECRET_KEY",
    paper=True,
)

await broker.connect()
```

### Notes

- `paper=True` is the safe default and should stay on until you are ready for live deployment
- The broker tracks positions and pending orders from Alpaca account state plus trade-update callbacks

## Recommended Wrapper

Wrap raw brokers with `SafeBroker` before handing them to `LiveEngine`:

```python
from ml4t.live import LiveRiskConfig, SafeBroker

safe_broker = SafeBroker(
    broker,
    LiveRiskConfig(
        execution_mode="shadow",
        max_position_value=25_000,
        max_order_value=5_000,
    ),
)
```

## Direct Async Broker Calls

Outside strategy code, use the broker asynchronously:

```python
cash = await broker.get_cash_async()
equity = await broker.get_account_value_async()
order = await broker.submit_order_async("AAPL", 10)
```

When `side` is omitted, quantity is signed: positive means buy and negative means sell. When
`side` is provided, quantity must be positive and unsigned. Zero, non-finite quantities, conflicting
signed quantities, invalid price combinations, and unsupported order types are rejected before the
broker SDK is called.

`CanonicalOrderRequest` performs that normalization once. The accepted side and magnitude then
remain unchanged through safety checks and adapter translation. Requests that fail validation do
not mutate rate history, counters, journals, virtual positions, or venue state.

## Capabilities And Reducing-Risk Orders

IB and Alpaca declare the order behaviors their adapters implement. Unsupported opening-auction,
stop, trailing, contingent, or other behavior fails before submission. `SafeBroker` treats an order
as reducing-risk only when the signed effect reduces an existing position without crossing through
zero. A sell from a flat account or a reversal is not exempt from normal exposure controls.

Cancel-and-resubmit replacement may leave an accepted cancellation without a replacement. That
condition is retained as a replacement gap and must be resolved through reconciliation. The runtime
does not report the original order as safely replaced.

## Strategy-Side Calls

Inside `Strategy.on_data(...)`, the broker object is synchronous:

```python
def on_data(self, timestamp, data, context, broker):
    if broker.get_position("AAPL") is None:
        broker.submit_order("AAPL", 10)
```

## Disconnect

```python
await broker.disconnect()
```

## Error Handling

Broker connection and order failures surface as standard Python exceptions, broker-SDK exceptions,
or `RiskLimitError` when wrapped by `SafeBroker`. An invalid broker snapshot blocks reconciliation
and startup. An accepted order followed by a persistence failure raises
`AcceptedOrderPersistenceError`; do not retry it until venue reconciliation establishes its state.

```python
from ml4t.live import RiskLimitError

try:
    await safe_broker.submit_order_async("AAPL", 10_000)
except RiskLimitError as exc:
    print(f"blocked by risk controls: {exc}")
```

For the surrounding migration story, read [Backtest to Live](backtest-to-live.md) and the
[Book Guide](../book-guide/index.md).
