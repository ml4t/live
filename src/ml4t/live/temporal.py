"""Shared calendar resolution, session labels, and trading time for Live."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from ml4t.backtest import BacktestConfig
from ml4t.backtest.calendar import get_calendar, get_schedule
from ml4t.backtest.sessions import session_date_for_timestamp
from ml4t.specs import UNSET_CALENDAR, BarLabel, CalendarUnset, FeedSpec, TimestampSemantics


def resolve_live_config(
    config: BacktestConfig | None,
    feed_spec: Any | None,
    calendar: str | None | CalendarUnset = UNSET_CALENDAR,
) -> BacktestConfig:
    """Merge shared feed declarations without mutating the caller's configuration."""
    spec = (
        FeedSpec.from_any(feed_spec) if feed_spec is not None else FeedSpec(time_contract_version=2)
    )
    if spec.timestamp_semantics is None:
        spec = spec.with_overrides(timestamp_semantics=TimestampSemantics.EVENT_TIME)
    resolved = (config or BacktestConfig()).merge_feed_spec(spec)
    if calendar is not UNSET_CALENDAR:
        resolved = replace(resolved, time_contract_version=2, calendar=calendar)
    return resolved


@dataclass(frozen=True, slots=True)
class _Session:
    label: date
    opening: datetime
    closing: datetime
    break_start: datetime | None
    break_end: datetime | None

    @property
    def intervals(self) -> tuple[tuple[datetime, datetime], ...]:
        if self.break_start is not None and self.break_end is not None:
            return ((self.opening, self.break_start), (self.break_end, self.closing))
        return ((self.opening, self.closing),)


@dataclass(frozen=True, slots=True)
class _Schedule:
    sessions: tuple[_Session, ...]
    openings: tuple[datetime, ...]
    starts: tuple[datetime, ...]
    ends: tuple[datetime, ...]
    labels: frozenset[date]


@lru_cache(maxsize=128)
def _schedule(calendar: str, year: int) -> _Schedule:
    frame = get_schedule(calendar, date(year, 1, 1), date(year, 12, 31), include_breaks=True)
    sessions = tuple(
        sorted(
            (
                _Session(
                    row["session_date"],
                    row["market_open"],
                    row["market_close"],
                    row.get("break_start"),
                    row.get("break_end"),
                )
                for row in frame.iter_rows(named=True)
            ),
            key=lambda session: session.opening,
        )
    )
    intervals = tuple(interval for session in sessions for interval in session.intervals)
    return _Schedule(
        sessions,
        tuple(session.opening for session in sessions),
        tuple(start for start, _ in intervals),
        tuple(end for _, end in intervals),
        frozenset(session.label for session in sessions),
    )


class LiveCalendar:
    """Consume the shared resolved contract and authoritative exchange schedules."""

    def __init__(self, feed_spec: FeedSpec, calendar: str | None) -> None:
        self.feed_spec = feed_spec
        self.calendar = calendar
        self.timezone = ZoneInfo(feed_spec.timezone or "UTC")
        self.exchange_timezone = self.timezone
        self.continuous = False
        if calendar is not None:
            try:
                provider = get_calendar(calendar)
            except (RuntimeError, ValueError, KeyError) as error:
                raise ValueError(f"invalid calendar {calendar!r}") from error
            self.exchange_timezone = ZoneInfo(str(provider.tz))
            self.continuous = provider.name == "24/7"

    def clock(self, timestamp: datetime) -> datetime:
        """Interpret a naive clock in the declared timezone, rejecting DST ambiguity."""
        if timestamp.tzinfo is not None:
            return timestamp.astimezone(UTC)
        first = timestamp.replace(tzinfo=self.timezone, fold=0)
        second = timestamp.replace(tzinfo=self.timezone, fold=1)
        if first.utcoffset() != second.utcoffset() or (
            first.astimezone(UTC).astimezone(self.timezone).replace(tzinfo=None) != timestamp
        ):
            raise ValueError("ambiguous or nonexistent local timestamp; supply an aware timestamp")
        return first.astimezone(UTC)

    def session_date(self, timestamp: datetime) -> date:
        """Use the same timestamp-to-session mapping as the public Backtest engine."""
        return session_date_for_timestamp(
            timestamp,
            calendar=self.calendar,
            timezone=self.feed_spec.timezone,
            session_start_time=self.feed_spec.session_start_time,
            data_frequency=self.feed_spec.data_frequency,
            timestamp_semantics=self.feed_spec.timestamp_semantics or TimestampSemantics.EVENT_TIME,
            bar_label=self.feed_spec.bar_label,
        )

    def is_session(self, label: date) -> bool:
        return self.calendar is None or label in _schedule(self.calendar, label.year).labels

    def allows_event(self, timestamp: datetime) -> bool:
        if self.calendar is None:
            return True
        if self.feed_spec.timestamp_semantics is TimestampSemantics.SESSION_LABEL:
            return self.is_session(timestamp.date())
        instant = self.clock(timestamp)
        right = (
            self.feed_spec.timestamp_semantics is TimestampSemantics.BAR_CLOSE
            or self.feed_spec.bar_label is BarLabel.RIGHT
        )
        for year in range(instant.year - 1, instant.year + 2):
            schedule = _schedule(self.calendar, year)
            index = (
                bisect_left(schedule.starts, instant)
                if right
                else bisect_right(schedule.starts, instant)
            ) - 1
            if index >= 0 and (
                instant <= schedule.ends[index] if right else instant < schedule.ends[index]
            ):
                return True
        return False

    def status(self, timestamp: datetime) -> dict[str, Any]:
        instant = self.clock(timestamp)
        if self.calendar is None:
            return {"market": "continuous", "next_boundary": None, "session_date": None}
        nearby = []
        for year in range(instant.year - 1, instant.year + 2):
            schedule = _schedule(self.calendar, year)
            index = bisect_right(schedule.openings, instant) - 1
            if index >= 0:
                nearby.append(schedule.sessions[index])
            if index + 1 < len(schedule.sessions):
                nearby.append(schedule.sessions[index + 1])
        for session in sorted(nearby, key=lambda value: value.opening):
            if session.opening <= instant < session.closing:
                if (
                    session.break_start is not None
                    and session.break_end is not None
                    and session.break_start <= instant < session.break_end
                ):
                    return {
                        "market": "break",
                        "next_boundary": session.break_end,
                        "session_date": session.label,
                    }
                return {
                    "market": "continuous" if self.continuous else "open",
                    "next_boundary": (
                        session.break_start
                        if session.break_start is not None and instant < session.break_start
                        else session.closing
                    ),
                    "session_date": session.label,
                }
            if instant < session.opening:
                same_open_date = instant.astimezone(self.exchange_timezone).date() == (
                    session.opening.astimezone(self.exchange_timezone).date()
                )
                return {
                    "market": "pre_open" if same_open_date else "closed",
                    "next_boundary": session.opening,
                    "session_date": None,
                }
        return {"market": "closed", "next_boundary": None, "session_date": None}

    def trading_seconds(self, start: datetime, end: datetime) -> float:
        """Count elapsed tradable time, excluding scheduled closures and breaks."""
        start, end = self.clock(start), self.clock(end)
        if self.calendar is None:
            return max(0.0, (end - start).total_seconds())
        seconds = 0.0
        for year in range(start.year - 1, end.year + 2):
            schedule = _schedule(self.calendar, year)
            first = bisect_right(schedule.ends, start)
            last = bisect_left(schedule.starts, end)
            for index in range(first, last):
                seconds += max(
                    0.0,
                    (
                        min(end, schedule.ends[index]) - max(start, schedule.starts[index])
                    ).total_seconds(),
                )
        return seconds
