"""ML4T Live Trading Platform.

Enable copy-paste Strategy class from backtesting to live trading with zero code changes.
"""

try:
    from ml4t.live._version import __version__
except ImportError:
    __version__ = "0.0.0.dev0"

from .brokers.alpaca import AlpacaBroker
from .brokers.ib import IBBroker
from .engine import LiveEngine
from .feeds.aggregator import BarAggregator, BarBuffer
from .feeds.alpaca_feed import AlpacaDataFeed
from .feeds.crypto_feed import CryptoFeed
from .feeds.databento_feed import DataBentoFeed
from .feeds.ib_feed import IBDataFeed
from .feeds.okx_feed import OKXFundingFeed
from .lifecycle import LifecycleInvocation, LiveLifecycleDispatcher, callback_trace
from .protocols import AsyncBrokerProtocol, BrokerProtocol, DataFeedProtocol
from .safety import (
    LiveRiskConfig,
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
    "CryptoFeed",
    "DataBentoFeed",
    "IBDataFeed",
    "OKXFundingFeed",
    # Engine
    "LifecycleInvocation",
    "LiveEngine",
    "LiveLifecycleDispatcher",
    "callback_trace",
    # Protocols
    "AsyncBrokerProtocol",
    "BrokerProtocol",
    "DataFeedProtocol",
    # Safety
    "LiveRiskConfig",
    "ReconciliationMismatchError",
    "RiskLimitError",
    "RiskState",
    "SafeBroker",
    "VirtualPortfolio",
    # Wrappers
    "ThreadSafeBrokerWrapper",
]
