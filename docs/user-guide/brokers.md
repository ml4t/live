# Brokers

Broker integrations for live trading.

## Interactive Brokers

The most comprehensive broker integration:

```python
from ml4t.live import IBBroker

broker = IBBroker(
    host="127.0.0.1",
    port=7497,      # 7497=paper, 7496=live
    client_id=1,
)
```

### Setup

1. Download TWS or IB Gateway from [Interactive Brokers](https://www.interactivebrokers.com/)
2. Enable API connections in settings
3. Configure the socket port

### Paper vs Live

```python
# Paper trading (port 7497)
paper_broker = IBBroker(port=7497)

# Live trading (port 7496) - BE CAREFUL!
live_broker = IBBroker(port=7496)
```

### Connection

```python
await broker.connect()
await broker.disconnect()
```

## Alpaca

Commission-free stock trading:

```python
from ml4t.live import AlpacaBroker

broker = AlpacaBroker(
    api_key="YOUR_API_KEY",
    secret_key="YOUR_SECRET_KEY",
    paper=True,  # Use paper trading
)
```

### Setup

1. Sign up at [Alpaca](https://alpaca.markets/)
2. Get API keys from the dashboard
3. Use paper=True for testing

### Paper vs Live

```python
# Paper trading
paper_broker = AlpacaBroker(
    api_key="...",
    secret_key="...",
    paper=True,
)

# Live trading
live_broker = AlpacaBroker(
    api_key="...",
    secret_key="...",
    paper=False,
)
```

## SafeBroker Wrapper

Always wrap brokers with SafeBroker for risk management:

```python
from ml4t.live import SafeBroker, LiveRiskConfig

config = LiveRiskConfig(
    shadow_mode=True,
    max_position_value=10_000,
)

# Wrap any broker
safe_broker = SafeBroker(IBBroker(), config)
```

## Order Submission

All brokers share the same order interface:

```python
# Market order
broker.submit_order("AAPL", 100)

# With order type (if supported)
broker.submit_order("AAPL", 100, order_type="limit", limit_price=150.00)
```

## Position Tracking

```python
# Get current positions
positions = await broker.get_positions()

# Get account value
account = await broker.get_account()
```

## Error Handling

```python
from ml4t.live import BrokerConnectionError

try:
    await broker.connect()
except BrokerConnectionError:
    print("Failed to connect to broker")
```
