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
from .protocols import AsyncBrokerProtocol, BrokerProtocol, DataFeedProtocol
from .safety import LiveRiskConfig, RiskLimitError, RiskState, SafeBroker, VirtualPortfolio
from .wrappers import ThreadSafeBrokerWrapper

__all__ = [
    # Brokers
    "AlpacaBroker",
    "IBBroker",
    # Data Feeds
    "AlpacaDataFeed",
    "BarAggregator",
    "BarBuffer",
    # Engine
    "LiveEngine",
    # Protocols
    "AsyncBrokerProtocol",
    "BrokerProtocol",
    "DataFeedProtocol",
    # Safety
    "LiveRiskConfig",
    "RiskLimitError",
    "RiskState",
    "SafeBroker",
    "VirtualPortfolio",
    # Wrappers
    "ThreadSafeBrokerWrapper",
]
