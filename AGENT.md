# ml4t-live

Live trading platform for ML4T strategies.

## Structure

| Directory | Purpose |
|-----------|---------|
| src/ml4t/live/ | Package root |
| tests/ | Test suite |

## Key Modules

| Module | Purpose |
|--------|---------|
| engine.py | Live trading engine |
| safety.py | Safety controls, kill switches |
| wrappers.py | Thread-safe broker wrapper |
| protocols.py | Broker/feed protocols |
| feeds/ | Real-time data feeds |
| brokers/ | Broker integrations |

## Entry Point

```python
from ml4t.live import LiveEngine
```
