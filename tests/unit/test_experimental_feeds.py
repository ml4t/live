"""Public-boundary tests for feeds outside the beta support contract."""

from inspect import signature
from pathlib import Path

import ml4t.live as live
import ml4t.live.feeds as feeds
from ml4t.live.feeds.crypto_feed import CryptoFeed
from ml4t.live.feeds.databento_feed import DataBentoFeed
from ml4t.live.feeds.experimental import ExperimentalFeedError, ExperimentalFeedWarning

ROOT = Path(__file__).parents[2]


def test_experimental_status_is_public_and_opt_in_defaults_to_false() -> None:
    assert live.ExperimentalFeedError is ExperimentalFeedError
    assert live.ExperimentalFeedWarning is ExperimentalFeedWarning
    assert feeds.ExperimentalFeedError is ExperimentalFeedError
    assert feeds.ExperimentalFeedWarning is ExperimentalFeedWarning
    assert CryptoFeed.support_status == "experimental"
    assert DataBentoFeed.support_status == "experimental"
    assert signature(CryptoFeed).parameters["experimental"].default is False
    assert signature(DataBentoFeed).parameters["experimental"].default is False


def test_public_claims_separate_experimental_feeds_from_beta_support() -> None:
    public_text = {
        "README": (ROOT / "README.md").read_text(),
        "feed guide": (ROOT / "docs/user-guide/feeds.md").read_text(),
        "API reference": (ROOT / "docs/api/index.md").read_text(),
        "docs landing": (ROOT / "docs/index.md").read_text(),
        "book guide": (ROOT / "docs/book-guide/index.md").read_text(),
    }

    assert (
        "Explicit opt-in experimental adapters for generic CCXT and DataBento"
        in public_text["README"]
    )
    assert "not part of the beta support contract" in public_text["feed guide"]
    assert "| Experimental feeds | `DataBentoFeed`, `CryptoFeed`" in public_text["API reference"]
    assert "experimental opt-in" in public_text["docs landing"]
    assert "experimental opt-in data source" in public_text["book guide"]
    assert "experimental=True" in public_text["README"]
    assert "experimental=True" in public_text["feed guide"]

    combined = "\n".join(public_text.values())
    for obsolete_claim in (
        "Six data feeds",
        "6 feed types",
        "retain the experimental tuple contract",
        "100+ crypto exchanges",
    ):
        assert obsolete_claim not in combined
