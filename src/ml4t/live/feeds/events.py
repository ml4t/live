"""Shared validation and strategy compatibility for typed feed events."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from ml4t.specs import EventCompletion, GapEvidence, MarketEvent, MarketEventKind


class FeedContractError(ValueError):
    """Raised when provider data cannot cross the portable feed boundary."""


class ContinuityDisposition(str, Enum):
    """Decision for one structurally valid market event."""

    ACCEPT = "accept"
    DUPLICATE = "duplicate"


class FeedContinuityError(FeedContractError):
    """Raised when event history cannot support another causal decision."""

    def __init__(self, reason: str, event: MarketEvent) -> None:
        self.reason = reason
        self.source = event.source
        self.asset = event.asset
        self.kind = event.kind.value
        self.provider_sequence = event.provider_sequence
        super().__init__(f"{event.source} {event.asset} {event.kind.value}: {reason}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "source": self.source,
            "asset": self.asset,
            "kind": self.kind,
            "provider_sequence": self.provider_sequence,
        }


@dataclass(frozen=True, slots=True)
class _ContinuityState:
    event: MarketEvent
    generation: int


class EventContinuityTracker:
    """Retain accepted identities across feed restart and reject unsafe continuation."""

    def __init__(self) -> None:
        self._states: dict[tuple[str, str, MarketEventKind], _ContinuityState] = {}
        self._generation = 0
        self._accepted_count = 0
        self._duplicate_count = 0
        self._violation_count = 0

    def mark_recovery(self) -> None:
        self._generation += 1

    def validate(self, event: MarketEvent) -> ContinuityDisposition:
        if event.gap is not None and event.gap.detected:
            return self._violation(event.gap.reason, event)

        key = (event.source, event.asset, event.kind)
        state = self._states.get(key)
        if state is None:
            self._accept(key, event)
            return ContinuityDisposition.ACCEPT

        previous = state.event
        exact_duplicate = (
            event.event_time == previous.event_time
            and event.provider_sequence == previous.provider_sequence
            and event.completion is previous.completion
            and event.payload == previous.payload
        )
        if exact_duplicate:
            self._duplicate_count += 1
            return ContinuityDisposition.DUPLICATE

        if event.event_time < previous.event_time:
            return self._violation("event time moved backwards", event)

        previous_sequence = previous.provider_sequence
        current_sequence = event.provider_sequence
        evolving_bar_revision = (
            event.kind is MarketEventKind.BAR
            and event.event_time == previous.event_time
            and previous.completion is EventCompletion.EVOLVING
        )
        if isinstance(previous_sequence, int) and isinstance(current_sequence, int):
            if current_sequence < previous_sequence:
                return self._violation("provider sequence replayed an older event", event)
            if current_sequence == previous_sequence and not evolving_bar_revision:
                return self._violation("provider sequence identifies conflicting events", event)
        elif (
            isinstance(previous_sequence, str)
            and isinstance(current_sequence, str)
            and current_sequence == previous_sequence
            and not evolving_bar_revision
        ):
            return self._violation("provider sequence identifies conflicting events", event)

        if event.event_time == previous.event_time:
            if event.kind is MarketEventKind.BAR:
                if previous.completion is EventCompletion.COMPLETE:
                    return self._violation("completed bar identity changed", event)
            elif current_sequence is None or previous_sequence is None:
                return self._violation(
                    "same-time snapshot has no distinct provider identity", event
                )

        if state.generation < self._generation:
            evidence = event.gap
            continuity_proved = (
                previous_sequence is not None
                and current_sequence is not None
                and evidence is not None
                and evidence.previous_sequence == str(previous_sequence)
                and evidence.current_sequence == str(current_sequence)
            )
            if not continuity_proved:
                return self._violation("provider continuity is unavailable after reconnect", event)

        self._accept(key, event)
        return ContinuityDisposition.ACCEPT

    def snapshot(self) -> dict[str, Any]:
        return {
            "generation": self._generation,
            "tracked_streams": len(self._states),
            "accepted_count": self._accepted_count,
            "duplicate_count": self._duplicate_count,
            "violation_count": self._violation_count,
            "last_sequences": {
                f"{source}:{asset}:{kind.value}": state.event.provider_sequence
                for (source, asset, kind), state in sorted(
                    self._states.items(),
                    key=lambda item: (item[0][0], item[0][1], item[0][2].value),
                )
            },
        }

    def _accept(
        self,
        key: tuple[str, str, MarketEventKind],
        event: MarketEvent,
    ) -> None:
        self._states[key] = _ContinuityState(event, self._generation)
        self._accepted_count += 1

    def _violation(
        self,
        reason: str,
        event: MarketEvent,
    ) -> ContinuityDisposition:
        self._violation_count += 1
        raise FeedContinuityError(reason, event)


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
    "ContinuityDisposition",
    "EventContinuityTracker",
    "FeedContinuityError",
    "FeedContractError",
    "sequence_unavailable",
    "strategy_input",
    "utc_datetime",
    "validate_event_timing",
]
