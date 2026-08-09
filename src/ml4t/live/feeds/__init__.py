"""Stable-supported and explicitly opt-in experimental data-feed components."""

from ml4t.live.feeds.aggregator import BarAggregator, BarBuffer
from ml4t.live.feeds.alpaca_feed import AlpacaDataFeed
from ml4t.live.feeds.crypto_feed import CryptoFeed
from ml4t.live.feeds.databento_feed import DataBentoFeed
from ml4t.live.feeds.events import FeedContinuityError, FeedContractError
from ml4t.live.feeds.experimental import ExperimentalFeedError, ExperimentalFeedWarning
from ml4t.live.feeds.ib_feed import IBDataFeed
from ml4t.live.feeds.okx_feed import OKXFundingFeed
from ml4t.live.feeds.queue import FeedOverflowError, FeedQueueSnapshot

__all__ = [
    "AlpacaDataFeed",
    "BarAggregator",
    "BarBuffer",
    "FeedContractError",
    "FeedContinuityError",
    "FeedOverflowError",
    "FeedQueueSnapshot",
    "IBDataFeed",
    "OKXFundingFeed",
    # Experimental opt-in surface
    "CryptoFeed",
    "DataBentoFeed",
    "ExperimentalFeedError",
    "ExperimentalFeedWarning",
]
