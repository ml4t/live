# API Reference

Use this page for signatures and public entry points. Use the User Guide for workflows, rollout
order, and broker/feed selection.

## Recommended Imports

```python
from ml4t.live import (
    AlpacaBroker,
    AlpacaDataFeed,
    AsyncBrokerProtocol,
    BarAggregator,
    BarBuffer,
    BrokerProtocol,
    BrokerOrderContractError,
    AcceptedOrderPersistenceError,
    AuditJournalError,
    CanonicalOrderRequest,
    ConcurrentStateWriterError,
    CorruptStateError,
    DataFeedProtocol,
    FeedContinuityError,
    FeedContractError,
    FeedOverflowError,
    FeedQueueSnapshot,
    IBBroker,
    IBDataFeed,
    LiveEngine,
    LiveRiskConfig,
    OKXFundingFeed,
    OrderReplacementGapError,
    OrderValidationError,
    PersistenceSafetyError,
    ReconciliationMismatchError,
    RiskLimitError,
    RiskState,
    RuntimeCleanupError,
    RuntimeErrorContext,
    RuntimeFailureError,
    RuntimeState,
    RuntimeTransition,
    SafeBroker,
    ThreadSafeBrokerWrapper,
    UnsafePersistencePathError,
    VirtualPortfolio,
    runtime_error_context,
)
```

## Public Surface At A Glance

| Group | Primary symbols |
| --- | --- |
| Engine | `LiveEngine`, `RuntimeState`, `RuntimeTransition`, `RuntimeErrorContext`, `runtime_error_context`, `RuntimeFailureError`, `RuntimeCleanupError` |
| Brokers | `IBBroker`, `AlpacaBroker` |
| Stable-supported feeds | `OKXFundingFeed` |
| Experimental feeds | `AlpacaDataFeed`, `IBDataFeed`, `DataBentoFeed`, `CryptoFeed`, `ExperimentalFeedError`, `ExperimentalFeedWarning` |
| Feed helpers | `BarAggregator`, `BarBuffer`, `FeedContractError`, `FeedContinuityError`, `FeedOverflowError`, `FeedQueueSnapshot` |
| Orders | `CanonicalOrderRequest`, `OrderValidationError`, `BrokerOrderContractError` |
| Safety | `LiveRiskConfig`, `SafeBroker`, `RiskState`, `RiskLimitError`, `PersistenceSafetyError`, `AuditJournalError`, `AcceptedOrderPersistenceError`, `VirtualPortfolio` |
| Sync/async bridge | `ThreadSafeBrokerWrapper` |
| Protocols | `BrokerProtocol`, `AsyncBrokerProtocol`, `DataFeedProtocol` |

## Engine

::: ml4t.live.engine.LiveEngine
    options:
      show_root_heading: true

::: ml4t.live.engine.RuntimeState
    options:
      show_root_heading: true

::: ml4t.live.engine.RuntimeTransition
    options:
      show_root_heading: true

::: ml4t.live.engine.RuntimeErrorContext
    options:
      show_root_heading: true

::: ml4t.live.engine.runtime_error_context
    options:
      show_root_heading: true

::: ml4t.live.engine.RuntimeFailureError
    options:
      show_root_heading: true

::: ml4t.live.engine.RuntimeCleanupError
    options:
      show_root_heading: true

## Safety And Rollout

::: ml4t.live.orders.CanonicalOrderRequest
    options:
      show_root_heading: true

::: ml4t.live.safety.LiveRiskConfig
    options:
      show_root_heading: true

::: ml4t.live.safety.SafeBroker
    options:
      show_root_heading: true

::: ml4t.live.wrappers.ThreadSafeBrokerWrapper
    options:
      show_root_heading: true

## Brokers

::: ml4t.live.brokers.ib.IBBroker
    options:
      show_root_heading: true

::: ml4t.live.brokers.alpaca.AlpacaBroker
    options:
      show_root_heading: true

## Data Feeds And Aggregation

::: ml4t.live.feeds.ib_feed.IBDataFeed
    options:
      show_root_heading: true

::: ml4t.live.feeds.alpaca_feed.AlpacaDataFeed
    options:
      show_root_heading: true

::: ml4t.live.feeds.okx_feed.OKXFundingFeed
    options:
      show_root_heading: true

::: ml4t.live.feeds.aggregator.BarAggregator
    options:
      show_root_heading: true

::: ml4t.live.feeds.aggregator.BarBuffer
    options:
      show_root_heading: true

::: ml4t.live.feeds.events.FeedContractError
    options:
      show_root_heading: true

::: ml4t.live.feeds.events.FeedContinuityError
    options:
      show_root_heading: true

::: ml4t.live.feeds.queue.FeedOverflowError
    options:
      show_root_heading: true

::: ml4t.live.feeds.queue.FeedQueueSnapshot
    options:
      show_root_heading: true

## Experimental Feed Opt-In

These imports are available for deliberate evaluation. They are not part of the stable support
contract and require `experimental=True`.

```python
from ml4t.live import (
    AlpacaDataFeed,
    CryptoFeed,
    DataBentoFeed,
    ExperimentalFeedError,
    ExperimentalFeedWarning,
    IBDataFeed,
)
```

::: ml4t.live.feeds.experimental.ExperimentalFeedError
    options:
      show_root_heading: true

::: ml4t.live.feeds.experimental.ExperimentalFeedWarning
    options:
      show_root_heading: true

::: ml4t.live.feeds.databento_feed.DataBentoFeed
    options:
      show_root_heading: true

::: ml4t.live.feeds.crypto_feed.CryptoFeed
    options:
      show_root_heading: true

## Protocols

::: ml4t.live.protocols.BrokerProtocol
    options:
      show_root_heading: true

::: ml4t.live.protocols.AsyncBrokerProtocol
    options:
      show_root_heading: true

::: ml4t.live.protocols.DataFeedProtocol
    options:
      show_root_heading: true

## Safety Types

::: ml4t.live.safety.RiskLimitError
    options:
      show_root_heading: true

::: ml4t.live.safety.ReconciliationMismatchError
    options:
      show_root_heading: true

::: ml4t.live.safety.RiskState
    options:
      show_root_heading: true

::: ml4t.live.safety.VirtualPortfolio
    options:
      show_root_heading: true

## Related Guides

- [Backtest to Live](../user-guide/backtest-to-live.md)
- [Brokers](../user-guide/brokers.md)
- [Data Feeds](../user-guide/feeds.md)
- [Risk Controls](../user-guide/risk.md)
- [Book Guide](../book-guide/index.md)
