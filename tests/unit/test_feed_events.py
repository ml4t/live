"""Tests for validation at the portable feed boundary."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from ml4t.specs import (
    EventCompletion,
    GapEvidence,
    LifecycleVersion,
    MarketEvent,
    MarketEventKind,
    QuotePayload,
)

from ml4t.live.feeds.events import (
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
    future_event = quote_event(event_time=now + timedelta(seconds=10), receipt_time=now)
    with pytest.raises(FeedContractError, match="after receipt_time"):
        validate_event_timing(future_event, processing_time=now, max_age_seconds=None)

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
