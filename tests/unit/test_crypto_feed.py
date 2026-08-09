"""Contract tests for the experimental generic CCXT feed."""

import asyncio
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from ml4t.specs import BarPayload, EventCompletion, MarketEventKind, TradePayload

from ml4t.live.feeds.crypto_feed import CryptoFeed
from ml4t.live.feeds.experimental import ExperimentalFeedError, ExperimentalFeedWarning


class MockExchange:
    def __init__(self) -> None:
        self.load_markets = AsyncMock()
        self.close = AsyncMock()


def make_feed(exchange: MockExchange | None = None, **kwargs: Any) -> CryptoFeed:
    exchange = exchange or MockExchange()
    with (
        patch("ml4t.live.feeds.crypto_feed.CCXT_AVAILABLE", True),
        patch("ml4t.live.feeds.crypto_feed.ccxt") as ccxt,
        pytest.warns(ExperimentalFeedWarning, match="Missing stable guarantees"),
    ):
        ccxt.binance.return_value = exchange
        return CryptoFeed(
            exchange="binance",
            symbols=["BTC/USDT"],
            experimental=True,
            **kwargs,
        )


def candle(timestamp_ms: int, close: float, volume: float = 10.0) -> list[float]:
    return [timestamp_ms, 100.0, max(110.0, close), 90.0, close, volume]


def drain(feed: CryptoFeed) -> list[Any]:
    items = []
    while not feed._queue.empty():
        items.append(feed._queue.get_nowait())
    return items


def test_constructor_requires_explicit_experimental_opt_in() -> None:
    with (
        patch("ml4t.live.feeds.crypto_feed.CCXT_AVAILABLE", True),
        patch("ml4t.live.feeds.crypto_feed.ccxt") as ccxt,
        pytest.raises(ExperimentalFeedError, match="experimental=True"),
    ):
        CryptoFeed(exchange="binance", symbols=["BTC/USDT"])

    ccxt.binance.assert_not_called()


def test_opt_in_reports_missing_guarantees_in_warning_and_stats() -> None:
    feed = make_feed()

    assert feed.stats["experimental"] is True
    assert feed.stats["missing_beta_guarantees"] == [
        "bounded overload behavior",
        "provider continuity across reconnect",
        "credentialed exchange qualification",
    ]


def test_missing_dependency_is_explicit_after_opt_in() -> None:
    with (
        patch("ml4t.live.feeds.crypto_feed.CCXT_AVAILABLE", False),
        pytest.warns(ExperimentalFeedWarning),
        pytest.raises(ImportError, match="ccxt package required"),
    ):
        CryptoFeed(exchange="binance", symbols=["BTC/USDT"], experimental=True)


def test_unknown_exchange_is_rejected_without_constructing_a_client() -> None:
    with (
        patch("ml4t.live.feeds.crypto_feed.CCXT_AVAILABLE", True),
        patch("ml4t.live.feeds.crypto_feed.ccxt") as ccxt,
        pytest.warns(ExperimentalFeedWarning),
        pytest.raises(ValueError, match="CCXT exchange is unavailable"),
    ):
        ccxt.unknown = None
        CryptoFeed(exchange="unknown", symbols=["BTC/USDT"], experimental=True)


@pytest.mark.parametrize(
    ("exchange", "symbols", "message"),
    [("", ["BTC/USDT"], "exchange"), ("binance", [], "symbols")],
)
def test_invalid_identity_fails_before_exchange_construction(
    exchange: str,
    symbols: list[str],
    message: str,
) -> None:
    with (
        patch("ml4t.live.feeds.crypto_feed.CCXT_AVAILABLE", True),
        patch("ml4t.live.feeds.crypto_feed.ccxt") as ccxt,
        pytest.warns(ExperimentalFeedWarning),
        pytest.raises(ValueError, match=message),
    ):
        CryptoFeed(exchange=exchange, symbols=symbols, experimental=True)

    ccxt.assert_not_called()


@pytest.mark.asyncio
async def test_trade_boundary_is_typed_utc_and_explicit() -> None:
    feed = make_feed()
    await feed._process_trade(
        {
            "timestamp": 1_704_067_200_000,
            "price": 50_000,
            "amount": 1.5,
            "side": "buy",
            "id": "trade-1",
        },
        "BTC/USDT",
    )

    event = feed._queue.get_nowait()
    assert event is not None
    assert event.event_time == datetime(2024, 1, 1, tzinfo=UTC)
    assert event.event_time.tzinfo is UTC
    assert event.kind is MarketEventKind.TRADE
    assert event.completion is EventCompletion.COMPLETE
    assert event.payload == TradePayload(50_000.0, 1.5)
    assert event.provider_sequence == "trade-1"
    assert event.metadata["experimental"] is True


@pytest.mark.asyncio
async def test_poll_batch_marks_only_prior_candle_complete() -> None:
    feed = make_feed()
    prior = candle(1_700_000_000_000, 105.0)
    current = candle(1_700_000_060_000, 119.0, 1.0)

    await feed._process_candle_batch([prior, current], "BTC/USDT")

    complete, evolving = drain(feed)
    assert complete.completion is EventCompletion.COMPLETE
    assert complete.payload == BarPayload(100.0, 110.0, 90.0, 105.0, 10.0)
    assert complete.event_time.tzinfo is UTC
    assert evolving.completion is EventCompletion.EVOLVING
    assert evolving.payload.close == 119.0


@pytest.mark.asyncio
async def test_final_revision_is_not_suppressed_by_timestamp_deduplication() -> None:
    feed = make_feed()
    timestamp = 1_700_000_060_000
    await feed._process_candle_batch(
        [candle(1_700_000_000_000, 105.0), candle(timestamp, 119.0, 1.0)],
        "BTC/USDT",
    )
    await feed._process_candle_batch(
        [candle(timestamp, 106.0, 12.0), candle(timestamp + 60_000, 107.0, 1.0)],
        "BTC/USDT",
    )
    await feed._process_candle_batch(
        [candle(timestamp, 106.0, 12.0), candle(timestamp + 60_000, 107.0, 1.0)],
        "BTC/USDT",
    )

    events = drain(feed)
    matching = [event for event in events if event.provider_sequence == str(timestamp)]
    assert [(event.completion, event.payload.close) for event in matching] == [
        (EventCompletion.EVOLVING, 119.0),
        (EventCompletion.COMPLETE, 106.0),
    ]


@pytest.mark.asyncio
async def test_invalid_candle_does_not_consume_its_completion_identity() -> None:
    feed = make_feed()
    timestamp = 1_700_000_060_000
    invalid = [timestamp, -1.0, 110.0, 90.0, 105.0, 10.0]

    with pytest.raises(ValueError):
        await feed._process_candle(
            invalid,
            "BTC/USDT",
            completion=EventCompletion.COMPLETE,
        )

    assert ("BTC/USDT", timestamp) not in feed._completed_candles
    await feed._process_candle(
        candle(timestamp, 105.0),
        "BTC/USDT",
        completion=EventCompletion.COMPLETE,
    )
    assert feed._queue.get_nowait() is not None


@pytest.mark.asyncio
async def test_single_websocket_candle_is_evolving_and_changed_revision_emits() -> None:
    feed = make_feed()
    timestamp = 1_700_000_060_000

    await feed._process_candle_batch([candle(timestamp, 105.0)], "BTC/USDT")
    await feed._process_candle_batch([candle(timestamp, 106.0)], "BTC/USDT")
    await feed._process_candle_batch([candle(timestamp, 106.0)], "BTC/USDT")

    events = drain(feed)
    assert [event.completion for event in events] == [
        EventCompletion.EVOLVING,
        EventCompletion.EVOLVING,
    ]
    assert [event.payload.close for event in events] == [105.0, 106.0]


@pytest.mark.asyncio
@pytest.mark.parametrize("websocket", [True, False])
async def test_stream_paths_apply_the_same_completion_policy(websocket: bool) -> None:
    exchange = MockExchange()
    batches = [
        [candle(1_700_000_000_000, 105.0), candle(1_700_000_060_000, 119.0)],
        [candle(1_700_000_060_000, 106.0), candle(1_700_000_120_000, 107.0)],
    ]

    async def next_batch(*args: Any, **kwargs: Any) -> list[list[float]]:
        if not batches:
            raise asyncio.CancelledError
        return batches.pop(0)

    if websocket:
        cast(Any, exchange).watch_ohlcv = next_batch
    else:
        cast(Any, exchange).fetch_ohlcv = next_batch
    feed = make_feed(exchange)
    feed._running = True

    with patch("asyncio.sleep", new=AsyncMock()):
        await feed._stream_ohlcv_for_symbol("BTC/USDT")

    events = drain(feed)
    final = [
        event
        for event in events
        if event.provider_sequence == "1700000060000"
        and event.completion is EventCompletion.COMPLETE
    ]
    assert len(final) == 1
    assert final[0].payload.close == 106.0


@pytest.mark.asyncio
async def test_stream_failure_wakes_consumer_with_cause() -> None:
    exchange = MockExchange()

    async def failed(*args: Any, **kwargs: Any) -> list[list[float]]:
        raise ConnectionError("provider unavailable")

    cast(Any, exchange).watch_ohlcv = failed
    feed = make_feed(exchange)
    feed._running = True

    await feed._stream_ohlcv_for_symbol("BTC/USDT")

    with pytest.raises(RuntimeError, match="experimental ohlcv") as raised:
        await anext(feed.__aiter__())
    assert isinstance(raised.value.__cause__, ConnectionError)


@pytest.mark.asyncio
async def test_start_stop_and_close_are_deterministic() -> None:
    exchange = MockExchange()
    feed = make_feed(exchange, stream_trades=True, stream_ohlcv=True)

    await feed.start()
    tasks = list(feed._stream_tasks)
    assert feed._running is True
    assert len(tasks) == 2
    exchange.load_markets.assert_awaited_once()

    feed.stop()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert feed._running is False
    assert feed._queue.get_nowait() is None

    await feed.close()
    exchange.close.assert_awaited_once()
