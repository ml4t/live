"""Shared validation and strategy compatibility for typed feed events."""

from __future__ import annotations

import math
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from ml4t.specs import GapEvidence, MarketEvent, MarketEventKind


class FeedContractError(ValueError):
    """Raised when provider data cannot cross the portable feed boundary."""


def utc_datetime(value: object, field: str) -> datetime:
    """Return an aware UTC datetime without silently assigning or converting a zone."""
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != UTC.utcoffset(value)
    ):
        raise FeedContractError(f"{field} must be timezone-aware UTC")
    return value.astimezone(UTC)


def sequence_unavailable(source: str, detail: str) -> GapEvidence:
    """Describe a provider's explicit lack of sequence capability."""
    return GapEvidence(False, f"{source} provider sequence unavailable: {detail}")


def validate_event_timing(
    event: MarketEvent,
    *,
    processing_time: datetime,
    max_age_seconds: float | None,
    future_tolerance_seconds: float = 5.0,
) -> None:
    """Reject invalid clock order and optionally stale events before strategy dispatch."""
    processing_time = utc_datetime(processing_time, "processing_time")
    if (
        isinstance(future_tolerance_seconds, bool)
        or not isinstance(future_tolerance_seconds, int | float)
        or not math.isfinite(future_tolerance_seconds)
        or future_tolerance_seconds < 0
    ):
        raise ValueError("future_tolerance_seconds must be finite and non-negative")
    tolerance = timedelta(seconds=future_tolerance_seconds)
    if event.event_time > event.receipt_time + tolerance:
        raise FeedContractError("event_time is after receipt_time")
    if event.receipt_time > processing_time + tolerance:
        raise FeedContractError("receipt_time is after processing_time")
    if max_age_seconds is None:
        return
    if (
        isinstance(max_age_seconds, bool)
        or not isinstance(max_age_seconds, int | float)
        or not math.isfinite(max_age_seconds)
        or max_age_seconds <= 0
    ):
        raise ValueError("max_age_seconds must be finite and positive or None")
    age = (processing_time - event.event_time).total_seconds()
    if age > max_age_seconds:
        raise FeedContractError(
            f"{event.source} {event.kind.value} event is stale: "
            f"{age:.3f}s exceeds {max_age_seconds:.3f}s"
        )


def strategy_input(
    event: MarketEvent,
    *,
    processing_time: datetime,
) -> tuple[datetime, dict[str, dict[str, Any]], dict[str, Any]]:
    """Adapt a typed event to the versioned strategy callback's current arguments."""
    payload = asdict(event.payload)
    if event.kind is MarketEventKind.QUOTE:
        payload["price"] = (payload["bid"] + payload["ask"]) / 2
    elif event.kind is MarketEventKind.FUNDING:
        payload = {"funding_rate": payload["rate"]}
    context = {
        event.asset: dict(event.metadata),
        "_market_event": {
            "version": event.version.value,
            "kind": event.kind.value,
            "completion": event.completion.value,
            "source": event.source,
            "provider_sequence": event.provider_sequence,
            "gap": asdict(event.gap) if event.gap is not None else None,
            "event_time": event.event_time,
            "receipt_time": event.receipt_time,
            "processing_time": utc_datetime(processing_time, "processing_time"),
        },
    }
    return event.event_time, {event.asset: payload}, context


__all__ = [
    "FeedContractError",
    "sequence_unavailable",
    "strategy_input",
    "utc_datetime",
    "validate_event_timing",
]
