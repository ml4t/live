"""ML4T Live Trading Platform.

Enable copy-paste Strategy class from backtesting to live trading with zero code changes.
"""

try:
    from ml4t.live._version import __version__
except ImportError:
    __version__ = "0.0.0.dev0"

from .brokers.alpaca import AlpacaBroker
from .brokers.ib import IBBroker
from .engine import (
    LiveEngine,
    RuntimeCleanupError,
    RuntimeFailureError,
    RuntimeState,
    RuntimeTransition,
)
from .feeds.aggregator import BarAggregator, BarBuffer
from .feeds.alpaca_feed import AlpacaDataFeed
from .feeds.crypto_feed import CryptoFeed
from .feeds.databento_feed import DataBentoFeed
from .feeds.events import FeedContinuityError, FeedContractError
from .feeds.experimental import ExperimentalFeedError, ExperimentalFeedWarning
from .feeds.ib_feed import IBDataFeed
from .feeds.okx_feed import OKXFundingFeed
from .feeds.queue import FeedOverflowError, FeedQueueSnapshot
from .lifecycle import LifecycleInvocation, LiveLifecycleDispatcher, callback_trace
from .orders import (
    BrokerOrderContractError,
    CanonicalOrderRequest,
    OrderValidationError,
    UnsupportedOrderCapabilityError,
)
from .persistence import (
    AcceptedOrderPersistenceError,
    AuditJournalError,
    ConcurrentStateWriterError,
    CorruptStateError,
    PersistenceSafetyError,
    UnsafePersistencePathError,
)
from .protocols import AsyncBrokerProtocol, BrokerProtocol, DataFeedProtocol
from .runtime import (
    LiveIntentError,
    LiveStrategyRuntime,
    LiveStrategyRuntimeError,
    ReducingRiskExecutionError,
    UnsupportedLiveCapabilityError,
    default_live_execution_policy,
)
from .safety import (
    BrokerSnapshotError,
    ExecutionMode,
    ExecutionModeError,
    LiveRiskConfig,
    OrderReplacementGapError,
    ReconciliationMismatchError,
    RiskLimitError,
    RiskState,
    SafeBroker,
    VirtualPortfolio,
)
from .wrappers import ThreadSafeBrokerWrapper

__all__ = [
    # Brokers
    "AlpacaBroker",
    "IBBroker",
    # Data Feeds
    "AlpacaDataFeed",
    "BarAggregator",
    "BarBuffer",
    "FeedContractError",
    "FeedContinuityError",
    "FeedOverflowError",
    "FeedQueueSnapshot",
    "IBDataFeed",
    "OKXFundingFeed",
    # Experimental Data Feeds
    "CryptoFeed",
    "DataBentoFeed",
    "ExperimentalFeedError",
    "ExperimentalFeedWarning",
    # Engine
    "LifecycleInvocation",
    "LiveEngine",
    "RuntimeCleanupError",
    "RuntimeFailureError",
    "RuntimeState",
    "RuntimeTransition",
    "BrokerOrderContractError",
    "CanonicalOrderRequest",
    "OrderValidationError",
    "UnsupportedOrderCapabilityError",
    "LiveLifecycleDispatcher",
    "callback_trace",
    "LiveIntentError",
    "LiveStrategyRuntime",
    "LiveStrategyRuntimeError",
    "ReducingRiskExecutionError",
    "UnsupportedLiveCapabilityError",
    "default_live_execution_policy",
    # Protocols
    "AsyncBrokerProtocol",
    "BrokerProtocol",
    "DataFeedProtocol",
    # Safety
    "AcceptedOrderPersistenceError",
    "AuditJournalError",
    "BrokerSnapshotError",
    "ExecutionMode",
    "ExecutionModeError",
    "ConcurrentStateWriterError",
    "CorruptStateError",
    "LiveRiskConfig",
    "OrderReplacementGapError",
    "PersistenceSafetyError",
    "ReconciliationMismatchError",
    "RiskLimitError",
    "RiskState",
    "SafeBroker",
    "UnsafePersistencePathError",
    "VirtualPortfolio",
    # Wrappers
    "ThreadSafeBrokerWrapper",
]
