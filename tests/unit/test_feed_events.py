"""Tests for validation at the portable feed boundary."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from ml4t.specs import (
    BarPayload,
    EventCompletion,
    GapEvidence,
    LifecycleVersion,
    MarketEvent,
    MarketEventKind,
    QuotePayload,
    TradePayload,
)

from ml4t.live.feeds.events import (
    ContinuityDisposition,
    EventContinuityTracker,
    FeedContinuityError,
    FeedContractError,
    sequence_unavailable,
    strategy_input,
    utc_datetime,
    validate_event_timing,
)


def quote_event(*, event_time: datetime, receipt_time: datetime) -> MarketEvent:
    return MarketEvent(
        version=LifecycleVersion.V1,
        event_time=event_time,
        receipt_time=receipt_time,
        kind=MarketEventKind.QUOTE,
        completion=EventCompletion.EVOLVING,
        source="fixture",
        asset="AAPL",
        payload=QuotePayload(149.0, 151.0, 10.0, 20.0),
        provider_sequence="quote-1",
        metadata={"venue": "test"},
    )


def test_utc_datetime_rejects_naive_and_non_utc_values() -> None:
    with pytest.raises(FeedContractError, match="timezone-aware UTC"):
        utc_datetime(datetime(2024, 1, 1), "timestamp")
    with pytest.raises(FeedContractError, match="timezone-aware UTC"):
        utc_datetime(datetime(2024, 1, 1, tzinfo=timezone(timedelta(hours=1))), "timestamp")

    value = datetime(2024, 1, 1, tzinfo=UTC)
    assert utc_datetime(value, "timestamp") is value


def test_timing_validation_rejects_reversed_clocks_and_staleness() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="receipt_time must not precede event_time"):
        quote_event(event_time=now + timedelta(seconds=10), receipt_time=now)

    future_receipt = quote_event(event_time=now, receipt_time=now + timedelta(seconds=10))
    with pytest.raises(FeedContractError, match="after processing_time"):
        validate_event_timing(future_receipt, processing_time=now, max_age_seconds=None)

    stale = quote_event(event_time=now - timedelta(seconds=31), receipt_time=now)
    with pytest.raises(FeedContractError, match="event is stale"):
        validate_event_timing(stale, processing_time=now, max_age_seconds=30)


@pytest.mark.parametrize("value", [True, 0, -1, float("nan"), float("inf")])
def test_timing_validation_rejects_invalid_age_configuration(value: object) -> None:
    now = datetime.now(UTC)
    event = quote_event(event_time=now, receipt_time=now)

    with pytest.raises(ValueError, match="finite and positive"):
        validate_event_timing(event, processing_time=now, max_age_seconds=value)  # type: ignore[arg-type]


def test_strategy_input_retains_identity_timing_and_capability() -> None:
    now = datetime.now(UTC)
    event = quote_event(event_time=now, receipt_time=now)

    timestamp, data, context = strategy_input(event, processing_time=now)

    assert timestamp == now
    assert data == {
        "AAPL": {
            "bid": 149.0,
            "ask": 151.0,
            "bid_size": 10.0,
            "ask_size": 20.0,
            "price": 150.0,
        }
    }
    assert context["AAPL"] == {"venue": "test"}
    assert context["_market_event"] == {
        "version": "1",
        "kind": "quote",
        "completion": "evolving",
        "source": "fixture",
        "provider_sequence": "quote-1",
        "gap": None,
        "event_time": now,
        "receipt_time": now,
        "processing_time": now,
    }


def test_sequence_unavailable_is_explicit_gap_evidence() -> None:
    assert sequence_unavailable("provider", "no sequence field") == GapEvidence(
        False,
        "provider provider sequence unavailable: no sequence field",
    )


def trade_event(
    *,
    timestamp: datetime,
    sequence: int | None,
    price: float = 150.0,
    gap: GapEvidence | None = None,
) -> MarketEvent:
    return MarketEvent(
        version=LifecycleVersion.V1,
        event_time=timestamp,
        receipt_time=timestamp,
        kind=MarketEventKind.TRADE,
        completion=EventCompletion.COMPLETE,
        source="fixture",
        asset="AAPL",
        payload=TradePayload(price, 1.0),
        provider_sequence=sequence,
        gap=gap,
    )


def test_continuity_accepts_monotonic_identity_and_skips_exact_duplicate() -> None:
    tracker = EventContinuityTracker()
    now = datetime.now(UTC)
    first = trade_event(timestamp=now, sequence=1)
    second = trade_event(timestamp=now + timedelta(seconds=1), sequence=2)

    assert tracker.validate(first) is ContinuityDisposition.ACCEPT
    assert tracker.validate(first) is ContinuityDisposition.DUPLICATE
    assert tracker.validate(second) is ContinuityDisposition.ACCEPT
    assert tracker.snapshot() == {
        "generation": 0,
        "tracked_streams": 1,
        "accepted_count": 2,
        "duplicate_count": 1,
        "violation_count": 0,
        "last_sequences": {"fixture:AAPL:trade": 2},
    }


def test_continuity_rejects_replay_explicit_gap_and_conflicting_identity() -> None:
    now = datetime.now(UTC)

    replay_tracker = EventContinuityTracker()
    replay_tracker.validate(trade_event(timestamp=now, sequence=2))
    with pytest.raises(FeedContinuityError, match="sequence replayed"):
        replay_tracker.validate(trade_event(timestamp=now + timedelta(seconds=1), sequence=1))

    gap_tracker = EventContinuityTracker()
    with pytest.raises(FeedContinuityError, match="provider reported missing sequence"):
        gap_tracker.validate(
            trade_event(
                timestamp=now,
                sequence=3,
                gap=GapEvidence(
                    True,
                    "provider reported missing sequence",
                    previous_sequence=1,
                    current_sequence=3,
                ),
            )
        )

    conflict_tracker = EventContinuityTracker()
    conflict_tracker.validate(trade_event(timestamp=now, sequence=1))
    with pytest.raises(FeedContinuityError, match="conflicting events"):
        conflict_tracker.validate(trade_event(timestamp=now, sequence=1, price=151.0))


def test_reconnect_requires_provable_continuity_but_skips_replayed_duplicate() -> None:
    tracker = EventContinuityTracker()
    now = datetime.now(UTC)
    unavailable = GapEvidence(False, "provider sequence unavailable")
    first = trade_event(timestamp=now, sequence=None, gap=unavailable)
    tracker.validate(first)
    tracker.mark_recovery()

    assert tracker.validate(first) is ContinuityDisposition.DUPLICATE
    with pytest.raises(FeedContinuityError, match="unavailable after reconnect"):
        tracker.validate(
            trade_event(
                timestamp=now + timedelta(seconds=1),
                sequence=None,
                gap=unavailable,
            )
        )

    sequenced = EventContinuityTracker()
    sequenced.validate(trade_event(timestamp=now, sequence=10))
    sequenced.mark_recovery()
    assert (
        sequenced.validate(
            trade_event(
                timestamp=now + timedelta(seconds=1),
                sequence=11,
                gap=GapEvidence(
                    False,
                    "provider continuity proved",
                    previous_sequence=10,
                    current_sequence=11,
                ),
            )
        )
        is ContinuityDisposition.ACCEPT
    )


def test_evolving_bar_revision_and_final_are_valid_but_completed_change_is_not() -> None:
    tracker = EventContinuityTracker()
    now = datetime.now(UTC)

    def bar(close: float, completion: EventCompletion) -> MarketEvent:
        return MarketEvent(
            version=LifecycleVersion.V1,
            event_time=now,
            receipt_time=now,
            kind=MarketEventKind.BAR,
            completion=completion,
            source="fixture",
            asset="AAPL",
            payload=BarPayload(149.0, 152.0, 148.0, close, 100.0),
            provider_sequence=1,
        )

    assert tracker.validate(bar(150.0, EventCompletion.EVOLVING)) is ContinuityDisposition.ACCEPT
    assert tracker.validate(bar(151.0, EventCompletion.EVOLVING)) is ContinuityDisposition.ACCEPT
    assert tracker.validate(bar(151.0, EventCompletion.COMPLETE)) is ContinuityDisposition.ACCEPT
    with pytest.raises(FeedContinuityError, match="conflicting events|completed bar"):
        tracker.validate(bar(150.0, EventCompletion.COMPLETE))
