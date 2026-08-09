"""Contract tests for the experimental DataBento feed."""

import asyncio
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from ml4t.specs import BarPayload, EventCompletion, MarketEventKind, QuotePayload, TradePayload

from ml4t.live.feeds.databento_feed import DataBentoFeed
from ml4t.live.feeds.events import FeedContractError
from ml4t.live.feeds.experimental import ExperimentalFeedError, ExperimentalFeedWarning


class MockRecord:
    def __init__(
        self,
        record_type: str = "ohlcv",
        symbol: str = "SPY",
        timestamp_ns: int = 1_704_067_200_000_000_000,
        sequence: int | None = None,
    ) -> None:
        self.symbol = symbol
        self.ts_event = timestamp_ns
        if sequence is not None:
            self.sequence = sequence
        if record_type == "ohlcv":
            self.open = int(450.0 * 1e9)
            self.high = int(451.0 * 1e9)
            self.low = int(449.0 * 1e9)
            self.close = int(450.5 * 1e9)
            self.volume = 10_000
        elif record_type == "trade":
            self.price = int(450.0 * 1e9)
            self.size = 100
        elif record_type == "mbp":
            self.bid_px_00 = int(449.99 * 1e9)
            self.ask_px_00 = int(450.01 * 1e9)
            self.bid_sz_00 = 200
            self.ask_sz_00 = 150


class MockDBNStore:
    def __init__(self, records: list[MockRecord]) -> None:
        self.records = iter(records)

    def __iter__(self) -> "MockDBNStore":
        return self

    def __next__(self) -> MockRecord:
        return next(self.records)


class MockLiveClient:
    def __init__(self, records: list[MockRecord], failure: Exception | None = None) -> None:
        self.records = iter(records)
        self.failure = failure

    def __aiter__(self) -> "MockLiveClient":
        return self

    async def __anext__(self) -> MockRecord:
        try:
            return next(self.records)
        except StopIteration:
            if self.failure is not None:
                raise self.failure from None
            raise StopAsyncIteration from None


def make_feed(client: Any, **kwargs: Any) -> DataBentoFeed:
    with (
        patch("ml4t.live.feeds.databento_feed.DATABENTO_AVAILABLE", True),
        pytest.warns(ExperimentalFeedWarning, match="Missing beta guarantees"),
    ):
        return DataBentoFeed(
            client,
            symbols=["SPY"],
            experimental=True,
            **kwargs,
        )


def test_constructor_requires_explicit_experimental_opt_in() -> None:
    with (
        patch("ml4t.live.feeds.databento_feed.DATABENTO_AVAILABLE", True),
        pytest.raises(ExperimentalFeedError, match="experimental=True"),
    ):
        DataBentoFeed(MockDBNStore([]), symbols=["SPY"])


def test_opt_in_reports_missing_guarantees_in_warning_and_stats() -> None:
    feed = make_feed(MockDBNStore([]))

    assert feed.stats["experimental"] is True
    assert feed.stats["missing_beta_guarantees"] == [
        "bounded overload behavior",
        "schema-wide causal qualification",
        "credentialed live-service qualification",
    ]


def test_missing_dependency_is_explicit_after_opt_in() -> None:
    with (
        patch("ml4t.live.feeds.databento_feed.DATABENTO_AVAILABLE", False),
        pytest.warns(ExperimentalFeedWarning),
        pytest.raises(ImportError, match=r"ml4t-live\[experimental\]"),
    ):
        DataBentoFeed(None, symbols=["SPY"], experimental=True)


def test_from_file_requires_opt_in_before_reading_the_file() -> None:
    with (
        patch("ml4t.live.feeds.databento_feed.DATABENTO_AVAILABLE", True),
        patch("ml4t.live.feeds.databento_feed.db") as databento,
        pytest.raises(ExperimentalFeedError, match="experimental=True"),
    ):
        DataBentoFeed.from_file("records.dbn", symbols=["SPY"])

    databento.DBNStore.from_file.assert_not_called()


def test_from_live_validates_symbols_before_creating_client() -> None:
    with (
        patch("ml4t.live.feeds.databento_feed.DATABENTO_AVAILABLE", True),
        patch("ml4t.live.feeds.databento_feed.db") as databento,
        pytest.raises(ValueError, match="symbols"),
    ):
        DataBentoFeed.from_live(
            api_key="secret",
            dataset="GLBX.MDP3",
            schema="trades",
            symbols=[],
            experimental=True,
        )

    databento.Live.assert_not_called()


def test_experimental_from_file_constructs_selected_replay() -> None:
    store = MockDBNStore([])
    with (
        patch("ml4t.live.feeds.databento_feed.DATABENTO_AVAILABLE", True),
        patch("ml4t.live.feeds.databento_feed.db") as databento,
        pytest.warns(ExperimentalFeedWarning),
    ):
        databento.DBNStore.from_file.return_value = store
        feed = DataBentoFeed.from_file(
            "records.dbn",
            symbols=["SPY"],
            replay_speed=0,
            experimental=True,
        )

    databento.DBNStore.from_file.assert_called_once_with("records.dbn")
    assert feed.client is store
    assert feed.mode == "historical"


def test_experimental_from_live_subscribes_exact_request() -> None:
    client = MockLiveClient([])
    cast(Any, client).subscribe = MagicMock()
    with (
        patch("ml4t.live.feeds.databento_feed.DATABENTO_AVAILABLE", True),
        patch("ml4t.live.feeds.databento_feed.db") as databento,
        pytest.warns(ExperimentalFeedWarning),
    ):
        databento.Live.return_value = client
        feed = DataBentoFeed.from_live(
            api_key="secret",
            dataset="GLBX.MDP3",
            schema="trades",
            symbols=["SPY"],
            experimental=True,
        )

    cast(Any, client).subscribe.assert_called_once_with(
        dataset="GLBX.MDP3",
        schema="trades",
        symbols=["SPY"],
    )
    assert feed.client is client
    assert feed.mode == "live"


@pytest.mark.parametrize(
    ("symbols", "mode", "speed", "message"),
    [
        ([], "historical", 1.0, "symbols"),
        (["SPY"], "other", 1.0, "mode"),
        (["SPY"], "historical", -1.0, "replay_speed"),
    ],
)
def test_invalid_configuration_is_rejected(
    symbols: list[str],
    mode: str,
    speed: float,
    message: str,
) -> None:
    with (
        patch("ml4t.live.feeds.databento_feed.DATABENTO_AVAILABLE", True),
        pytest.warns(ExperimentalFeedWarning),
        pytest.raises(ValueError, match=message),
    ):
        DataBentoFeed(
            MockDBNStore([]),
            symbols=symbols,
            mode=mode,
            replay_speed=speed,
            experimental=True,
        )


@pytest.mark.parametrize(
    ("record", "kind", "completion", "payload"),
    [
        (
            MockRecord("ohlcv", sequence=1),
            MarketEventKind.BAR,
            EventCompletion.COMPLETE,
            BarPayload(450.0, 451.0, 449.0, 450.5, 10_000.0),
        ),
        (
            MockRecord("trade", sequence=2),
            MarketEventKind.TRADE,
            EventCompletion.COMPLETE,
            TradePayload(450.0, 100.0),
        ),
        (
            MockRecord("mbp", sequence=3),
            MarketEventKind.QUOTE,
            EventCompletion.EVOLVING,
            QuotePayload(449.99, 450.01, 200.0, 150.0),
        ),
    ],
)
def test_record_boundary_is_typed_utc_and_explicit(
    record: MockRecord,
    kind: MarketEventKind,
    completion: EventCompletion,
    payload: BarPayload | TradePayload | QuotePayload,
) -> None:
    feed = make_feed(MockDBNStore([]))

    event = feed._convert_record(record)

    assert event.event_time == datetime(2024, 1, 1, tzinfo=UTC)
    assert event.event_time.tzinfo is UTC
    assert event.kind is kind
    assert event.completion is completion
    assert event.payload == payload
    assert event.provider_sequence == record.sequence
    assert event.metadata["experimental"] is True


def test_unknown_schema_fails_instead_of_emitting_empty_data() -> None:
    feed = make_feed(MockDBNStore([]))

    with pytest.raises(FeedContractError, match="unsupported DataBento record schema"):
        feed._convert_record(MockRecord("unknown"))


def test_missing_symbol_fails_instead_of_using_placeholder_asset() -> None:
    feed = make_feed(MockDBNStore([]))
    record = MockRecord("trade")
    del record.symbol

    with pytest.raises(FeedContractError, match="non-empty symbol"):
        feed._convert_record(record)


@pytest.mark.asyncio
async def test_historical_replay_emits_typed_events_in_order() -> None:
    records = [
        MockRecord("ohlcv", timestamp_ns=1_704_067_200_000_000_000, sequence=1),
        MockRecord("trade", timestamp_ns=1_704_067_201_000_000_000, sequence=2),
    ]
    feed = make_feed(MockDBNStore(records), replay_speed=0)

    await feed.start()
    events = [event async for event in feed]

    assert [event.kind for event in events] == [MarketEventKind.BAR, MarketEventKind.TRADE]
    assert feed.stats["record_count"] == 2


@pytest.mark.asyncio
async def test_historical_replay_respects_configured_speed() -> None:
    records = [
        MockRecord(timestamp_ns=1_704_067_200_000_000_000),
        MockRecord(timestamp_ns=1_704_067_201_000_000_000),
    ]
    feed = make_feed(MockDBNStore(records), replay_speed=10)

    started = asyncio.get_running_loop().time()
    await feed.start()
    events = [event async for event in feed]

    assert len(events) == 2
    assert 0.08 <= asyncio.get_running_loop().time() - started < 0.5


@pytest.mark.asyncio
async def test_live_stream_failure_wakes_consumer_with_cause() -> None:
    feed = make_feed(
        MockLiveClient([MockRecord("trade", sequence=1)], ConnectionError("disconnected")),
        mode="live",
    )

    await feed.start()
    iterator = feed.__aiter__()
    first = await anext(iterator)
    assert first.kind is MarketEventKind.TRADE
    with pytest.raises(RuntimeError, match="experimental live") as raised:
        await anext(iterator)
    assert isinstance(raised.value.__cause__, ConnectionError)


@pytest.mark.asyncio
async def test_stop_terminates_iteration_and_cancels_task() -> None:
    feed = make_feed(MockDBNStore([MockRecord() for _ in range(100)]), replay_speed=1)
    await feed.start()
    task = feed._replay_task

    feed.stop()
    assert task is not None
    await asyncio.gather(task, return_exceptions=True)

    assert feed._running is False
    assert feed._queue.get_nowait() is None
