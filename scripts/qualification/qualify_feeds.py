"""Qualify stable external feeds against an exact installed candidate wheel."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import os
import sys
import time
import warnings
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

_paper = importlib.import_module("scripts.qualification.qualify_paper")
_candidate_identity = _paper._candidate_identity
_load_json = _paper._load_json
_verify_installed_candidate = _paper._verify_installed_candidate
_write_json = _paper._write_json

OKX_API = "https://www.okx.com/api/v5"
OKX_SYMBOL = "BTC-USDT-SWAP"
SOAK_DURATION_SECONDS = 6 * 60 * 60
SOAK_SNAPSHOT_INTERVAL_SECONDS = 5 * 60
SOAK_RSS_GROWTH_LIMIT_BYTES = 25 * 1024 * 1024
REQUIRED_STEPS = {
    "installed_candidate",
    "public_service_identity",
    "connect",
    "event_validation",
    "utc_timing",
    "completion",
    "provider_native_comparison",
    "continuity",
    "reconnect",
    "stale_rejection",
    "overload_fail_closed",
    "bounded_shutdown",
}


class FeedQualificationError(RuntimeError):
    """The candidate feed qualification contract failed."""


def _utc(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == UTC.utcoffset(value)


def _validate_okx_event(event: Any) -> None:
    from ml4t.specs import EventCompletion, MarketEventKind

    if event.source != "okx" or event.asset != OKX_SYMBOL:
        raise FeedQualificationError("adapter emitted an unexpected source or instrument")
    if not _utc(event.event_time) or not _utc(event.receipt_time):
        raise FeedQualificationError("adapter event is not UTC")
    if event.event_time > event.receipt_time + timedelta(seconds=5):
        raise FeedQualificationError("adapter event time follows receipt time")
    if event.provider_sequence is None:
        raise FeedQualificationError("stable OKX event has no provider sequence")
    if event.kind is MarketEventKind.BAR and event.completion not in {
        EventCompletion.COMPLETE,
        EventCompletion.EVOLVING,
    }:
        raise FeedQualificationError("OKX bar has an invalid completion state")
    if event.kind is MarketEventKind.FUNDING and event.completion is not EventCompletion.COMPLETE:
        raise FeedQualificationError("OKX funding event is not complete")


def _event_sequence(event: Any) -> int:
    sequence = event.provider_sequence
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        raise FeedQualificationError("OKX event sequence is not an integer")
    return sequence


async def _native_snapshot(client: Any) -> dict[str, Any]:
    paths = {
        "instrument": ("/public/instruments", {"instType": "SWAP", "instId": OKX_SYMBOL}),
        "candles": ("/market/candles", {"instId": OKX_SYMBOL, "bar": "1m", "limit": "10"}),
        "funding": ("/public/funding-rate", {"instId": OKX_SYMBOL}),
    }
    payloads: dict[str, Any] = {}
    for name, (path, params) in paths.items():
        response = await client.get(f"{OKX_API}{path}", params=params)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != "0" or not payload.get("data"):
            raise FeedQualificationError(f"OKX {name} endpoint did not identify the service")
        payloads[name] = payload["data"]
    if not any(
        item.get("instId") == OKX_SYMBOL and item.get("instType") == "SWAP"
        for item in payloads["instrument"]
    ):
        raise FeedQualificationError("OKX instrument identity did not match the requested swap")
    return payloads


def _native_matches(events: list[Any], native: dict[str, Any]) -> bool:
    from ml4t.specs import MarketEventKind

    bars = [event for event in events if event.kind is MarketEventKind.BAR]
    funding = [event for event in events if event.kind is MarketEventKind.FUNDING]
    candle_sequences = {int(row[0]) for row in native["candles"] if len(row) > 8}
    funding_sequences = {
        int(row["fundingTime"]) for row in native["funding"] if row.get("fundingTime")
    }
    return (
        bool(bars and funding)
        and all(_event_sequence(event) in candle_sequences for event in bars)
        and all(_event_sequence(event) in funding_sequences for event in funding)
    )


async def _collect_initial(feed: Any) -> list[Any]:
    from ml4t.specs import EventCompletion, MarketEventKind

    events: list[Any] = []
    async with asyncio.timeout(120):
        while not (
            any(
                event.kind is MarketEventKind.BAR and event.completion is EventCompletion.COMPLETE
                for event in events
            )
            and any(event.kind is MarketEventKind.FUNDING for event in events)
        ):
            event = await anext(feed)
            _validate_okx_event(event)
            events.append(event)
    return events


async def _collect_next_complete(feed: Any, previous_sequence: int) -> Any:
    from ml4t.specs import EventCompletion, MarketEventKind

    async with asyncio.timeout(150):
        while True:
            event = await anext(feed)
            _validate_okx_event(event)
            if (
                event.kind is MarketEventKind.BAR
                and event.completion is EventCompletion.COMPLETE
                and _event_sequence(event) > previous_sequence
            ):
                return event


async def _close_within_limit(feed: Any) -> float:
    started = time.monotonic()
    feed.stop()
    await feed.close()
    elapsed = time.monotonic() - started
    if elapsed >= 5:
        raise FeedQualificationError("feed shutdown exceeded five seconds")
    return elapsed


async def _qualify_overload() -> dict[str, Any]:
    from ml4t.live import OKXFundingFeed
    from ml4t.live.feeds.queue import FeedOverflowError

    feed = OKXFundingFeed(
        [OKX_SYMBOL, "ETH-USDT-SWAP"],
        timeframe="1m",
        poll_interval_seconds=1,
        queue_capacity=1,
    )
    await feed.start()
    try:
        async with asyncio.timeout(60):
            while feed.stats["queue"]["overflow_count"] == 0:
                await asyncio.sleep(0.1)
        try:
            await anext(feed)
        except FeedOverflowError:
            pass
        else:
            raise FeedQualificationError("feed overload did not fail the consumer")
        snapshot = feed.stats["queue"]
        if snapshot["overflow_count"] == 0 or not snapshot["failed"]:
            raise FeedQualificationError("feed overload was not observable")
        return {
            "failed_closed": True,
            "overflow_count": snapshot["overflow_count"],
            "retained_occupancy": snapshot["occupancy"],
        }
    finally:
        await _close_within_limit(feed)


async def qualify_okx(candidate: dict[str, Any], checkout_root: Path) -> dict[str, Any]:
    """Run the stable OKX contract against the public service."""
    import httpx
    from ml4t.specs import EventCompletion, MarketEventKind

    from ml4t.live import OKXFundingFeed
    from ml4t.live.feeds.events import FeedContractError, validate_event_timing

    identity = _candidate_identity(candidate)
    _verify_installed_candidate(identity, checkout_root)
    started_at = datetime.now(UTC)
    steps = ["installed_candidate"]

    async with httpx.AsyncClient(timeout=30) as client:
        native_before = await _native_snapshot(client)
        steps.append("public_service_identity")

        feed = OKXFundingFeed(
            [OKX_SYMBOL], timeframe="1m", poll_interval_seconds=1, queue_capacity=16
        )
        await feed.start()
        steps.append("connect")
        initial_events = await _collect_initial(feed)
        steps.extend(["event_validation", "utc_timing", "completion"])
        native_after = await _native_snapshot(client)
        if not (
            _native_matches(initial_events, native_before)
            or _native_matches(initial_events, native_after)
        ):
            raise FeedQualificationError("adapter events differ from provider-native observations")
        steps.append("provider_native_comparison")

        previous = next(
            event
            for event in initial_events
            if event.kind is MarketEventKind.BAR and event.completion is EventCompletion.COMPLETE
        )
        shutdown_seconds = await _close_within_limit(feed)
        await feed.start()
        previous_sequence = _event_sequence(previous)
        reconnected = await _collect_next_complete(feed, previous_sequence)
        expected_sequence = previous_sequence + 60_000
        if _event_sequence(reconnected) != expected_sequence:
            raise FeedQualificationError("OKX reconnect missed a complete candle interval")
        if reconnected.gap is None or reconnected.gap.detected:
            raise FeedQualificationError("OKX reconnect did not prove interval continuity")
        steps.extend(["continuity", "reconnect"])

        old = datetime.now(UTC) - timedelta(minutes=10)
        stale = replace(reconnected, event_time=old, receipt_time=old)
        try:
            validate_event_timing(
                stale,
                processing_time=datetime.now(UTC),
                max_age_seconds=feed.max_event_age_seconds,
            )
        except FeedContractError:
            pass
        else:
            raise FeedQualificationError("stale external event passed timing validation")
        steps.append("stale_rejection")
        shutdown_seconds = max(shutdown_seconds, await _close_within_limit(feed))

    overload = await _qualify_overload()
    steps.extend(["overload_fail_closed", "bounded_shutdown"])
    return {
        "schema_version": 1,
        "provider": "okx",
        "candidate": identity,
        "started_at": started_at.isoformat(),
        "completed_at": datetime.now(UTC).isoformat(),
        "endpoint": {
            "authentication": "public",
            "host": "www.okx.com",
            "instrument_type": "SWAP",
            "identity_verified": True,
        },
        "steps_passed": steps,
        "event_kinds": sorted({event.kind.value for event in initial_events}),
        "complete_interval_seconds": 60,
        "native_comparison_exact": True,
        "reconnect_continuity": True,
        "stale_rejected": True,
        "overload": overload,
        "maximum_shutdown_seconds": round(shutdown_seconds, 3),
        "passed": True,
    }


def validate_okx_report(report: dict[str, Any], candidate: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "provider",
        "candidate",
        "started_at",
        "completed_at",
        "endpoint",
        "steps_passed",
        "event_kinds",
        "complete_interval_seconds",
        "native_comparison_exact",
        "reconnect_continuity",
        "stale_rejected",
        "overload",
        "maximum_shutdown_seconds",
        "passed",
    }
    if set(report) != required or report.get("schema_version") != 1:
        raise FeedQualificationError("OKX report schema is invalid")
    if report.get("provider") != "okx" or report.get("candidate") != _candidate_identity(candidate):
        raise FeedQualificationError("OKX report targets a different provider or candidate")
    steps = report.get("steps_passed", [])
    if (
        not isinstance(steps, list)
        or len(steps) != len(REQUIRED_STEPS)
        or set(steps) != REQUIRED_STEPS
    ):
        raise FeedQualificationError("OKX report is missing mandatory operations")
    if set(report.get("event_kinds", [])) != {"bar", "funding"}:
        raise FeedQualificationError("OKX report is missing an event kind")
    endpoint = report.get("endpoint", {})
    overload = report.get("overload", {})
    started_at = _paper._evidence_time(report.get("started_at"))
    completed_at = _paper._evidence_time(report.get("completed_at"))
    shutdown = report.get("maximum_shutdown_seconds")
    if (
        endpoint
        != {
            "authentication": "public",
            "host": "www.okx.com",
            "instrument_type": "SWAP",
            "identity_verified": True,
        }
        or completed_at < started_at
        or report.get("complete_interval_seconds") != 60
        or report.get("native_comparison_exact") is not True
        or report.get("reconnect_continuity") is not True
        or report.get("stale_rejected") is not True
        or set(overload) != {"failed_closed", "overflow_count", "retained_occupancy"}
        or overload.get("failed_closed") is not True
        or not isinstance(overload.get("overflow_count"), int)
        or overload["overflow_count"] < 1
        or overload.get("retained_occupancy") != 0
        or report.get("passed") is not True
        or isinstance(shutdown, bool)
        or not isinstance(shutdown, int | float)
        or not 0 <= shutdown < 5
    ):
        raise FeedQualificationError("OKX report did not pass fail-closed qualification")


def _rss_bytes() -> int:
    fields = Path("/proc/self/statm").read_text().split()
    if len(fields) < 2:
        raise FeedQualificationError("Linux process RSS is unavailable")
    return int(fields[1]) * os.sysconf("SC_PAGE_SIZE")


def _soak_snapshot(feed: Any, *, started: float, event_state: dict[str, int]) -> dict[str, int]:
    stats = feed.stats
    queue = stats["queue"]
    return {
        "elapsed_seconds": round(time.monotonic() - started),
        "rss_bytes": _rss_bytes(),
        "event_count": event_state["event_count"],
        "complete_bar_count": event_state["complete_bar_count"],
        "funding_count": event_state["funding_count"],
        "error_count": stats["error_count"],
        "rejected_count": stats["rejected_count"],
        "overflow_count": queue["overflow_count"],
        "queue_high_watermark": queue["high_watermark"],
    }


async def qualify_okx_soak(candidate: dict[str, Any], checkout_root: Path) -> dict[str, Any]:
    """Run a six-hour continuous OKX session with one retained-state restart."""
    import httpx
    from ml4t.specs import EventCompletion, MarketEventKind

    from ml4t.live import OKXFundingFeed
    from ml4t.live.feeds.events import validate_event_timing

    identity = _candidate_identity(candidate)
    _verify_installed_candidate(identity, checkout_root)
    started_at = datetime.now(UTC)
    started = time.monotonic()
    deadline = started + SOAK_DURATION_SECONDS
    reconnect_at = started + SOAK_DURATION_SECONDS / 2
    next_snapshot = started + SOAK_SNAPSHOT_INTERVAL_SECONDS
    reconnected = False
    last_complete_sequence: int | None = None
    shutdown_seconds = 0.0
    checksum = hashlib.sha256()
    event_state = {"event_count": 0, "complete_bar_count": 0, "funding_count": 0}
    snapshots: list[dict[str, int]] = []
    feed = OKXFundingFeed([OKX_SYMBOL], timeframe="1m", poll_interval_seconds=5, queue_capacity=256)
    await feed.start()
    snapshots.append(_soak_snapshot(feed, started=started, event_state=event_state))
    try:
        while time.monotonic() < deadline:
            now = time.monotonic()
            if not reconnected and now >= reconnect_at:
                shutdown_seconds = max(shutdown_seconds, await _close_within_limit(feed))
                await feed.start()
                reconnected = True
                continue
            if now >= next_snapshot:
                snapshots.append(_soak_snapshot(feed, started=started, event_state=event_state))
                next_snapshot += SOAK_SNAPSHOT_INTERVAL_SECONDS
                continue
            wait_seconds = min(
                1.0,
                deadline - now,
                next_snapshot - now,
                reconnect_at - now if not reconnected else 1.0,
            )
            try:
                event = await asyncio.wait_for(anext(feed), timeout=max(wait_seconds, 0.01))
            except TimeoutError:
                continue
            _validate_okx_event(event)
            validate_event_timing(
                event,
                processing_time=datetime.now(UTC),
                max_age_seconds=feed.max_event_age_seconds,
            )
            event_state["event_count"] += 1
            checksum.update(
                f"{event.kind.value}|{event.completion.value}|{_event_sequence(event)}\n".encode()
            )
            if event.kind is MarketEventKind.FUNDING:
                event_state["funding_count"] += 1
            elif event.completion is EventCompletion.COMPLETE:
                sequence = _event_sequence(event)
                if (
                    last_complete_sequence is not None
                    and sequence != last_complete_sequence + 60_000
                ):
                    raise FeedQualificationError("OKX soak observed a candle continuity gap")
                if event.gap is not None and event.gap.detected:
                    raise FeedQualificationError("OKX soak received explicit gap evidence")
                last_complete_sequence = sequence
                event_state["complete_bar_count"] += 1
    finally:
        shutdown_seconds = max(shutdown_seconds, await _close_within_limit(feed))

    duration = time.monotonic() - started
    snapshots.append(_soak_snapshot(feed, started=started, event_state=event_state))
    async with httpx.AsyncClient(timeout=30) as client:
        native = await _native_snapshot(client)
    native_sequences = {int(row[0]) for row in native["candles"] if len(row) > 8}
    native_reconciled = (
        last_complete_sequence is not None and last_complete_sequence in native_sequences
    )
    rss_growth = max(snapshot["rss_bytes"] for snapshot in snapshots) - snapshots[0]["rss_bytes"]
    final_stats = feed.stats
    report = {
        "schema_version": 1,
        "provider": "okx",
        "candidate": identity,
        "started_at": started_at.isoformat(),
        "completed_at": datetime.now(UTC).isoformat(),
        "duration_seconds": round(duration, 3),
        "snapshot_interval_seconds": SOAK_SNAPSHOT_INTERVAL_SECONDS,
        "snapshots": snapshots,
        **event_state,
        "event_checksum": checksum.hexdigest(),
        "reconnect_count": int(reconnected),
        "continuity_gap_count": 0,
        "native_final_reconciliation": native_reconciled,
        "rss_growth_bytes": max(0, rss_growth),
        "maximum_shutdown_seconds": round(shutdown_seconds, 3),
        "error_count": final_stats["error_count"],
        "rejected_count": final_stats["rejected_count"],
        "overflow_count": final_stats["queue"]["overflow_count"],
        "passed": True,
    }
    validate_soak_report(report, candidate)
    return report


def validate_soak_report(report: dict[str, Any], candidate: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "provider",
        "candidate",
        "started_at",
        "completed_at",
        "duration_seconds",
        "snapshot_interval_seconds",
        "snapshots",
        "event_count",
        "complete_bar_count",
        "funding_count",
        "event_checksum",
        "reconnect_count",
        "continuity_gap_count",
        "native_final_reconciliation",
        "rss_growth_bytes",
        "maximum_shutdown_seconds",
        "error_count",
        "rejected_count",
        "overflow_count",
        "passed",
    }
    if set(report) != required or report.get("schema_version") != 1:
        raise FeedQualificationError("OKX soak report schema is invalid")
    if report.get("provider") != "okx" or report.get("candidate") != _candidate_identity(candidate):
        raise FeedQualificationError("OKX soak report targets a different provider or candidate")
    started_at = _paper._evidence_time(report.get("started_at"))
    completed_at = _paper._evidence_time(report.get("completed_at"))
    duration = report.get("duration_seconds")
    shutdown = report.get("maximum_shutdown_seconds")
    snapshots = report.get("snapshots")
    snapshot_fields = {
        "elapsed_seconds",
        "rss_bytes",
        "event_count",
        "complete_bar_count",
        "funding_count",
        "error_count",
        "rejected_count",
        "overflow_count",
        "queue_high_watermark",
    }
    minimum_snapshots = SOAK_DURATION_SECONDS // SOAK_SNAPSHOT_INTERVAL_SECONDS + 1
    count_fields = (
        "event_count",
        "complete_bar_count",
        "funding_count",
        "reconnect_count",
        "continuity_gap_count",
        "rss_growth_bytes",
        "error_count",
        "rejected_count",
        "overflow_count",
    )
    if (
        completed_at < started_at
        or isinstance(duration, bool)
        or not isinstance(duration, int | float)
        or duration < SOAK_DURATION_SECONDS
        or report.get("snapshot_interval_seconds") != SOAK_SNAPSHOT_INTERVAL_SECONDS
        or not isinstance(snapshots, list)
        or len(snapshots) < minimum_snapshots
        or any(
            isinstance(report.get(field), bool)
            or not isinstance(report.get(field), int)
            or report[field] < 0
            for field in count_fields
        )
        or not _paper.HASH_PATTERN.fullmatch(str(report.get("event_checksum", "")))
        or report.get("reconnect_count") != 1
        or report.get("continuity_gap_count") != 0
        or report.get("native_final_reconciliation") is not True
        or report.get("rss_growth_bytes", SOAK_RSS_GROWTH_LIMIT_BYTES)
        >= SOAK_RSS_GROWTH_LIMIT_BYTES
        or isinstance(shutdown, bool)
        or not isinstance(shutdown, int | float)
        or not 0 <= shutdown < 5
        or any(
            report.get(field) != 0 for field in ("error_count", "rejected_count", "overflow_count")
        )
        or report.get("passed") is not True
    ):
        raise FeedQualificationError("OKX soak did not pass the stable provider contract")
    if any(
        not isinstance(snapshot, dict) or set(snapshot) != snapshot_fields for snapshot in snapshots
    ):
        raise FeedQualificationError("OKX soak snapshot schema is invalid")
    if any(
        isinstance(snapshot[field], bool)
        or not isinstance(snapshot[field], int)
        or snapshot[field] < 0
        for snapshot in snapshots
        for field in snapshot_fields
    ):
        raise FeedQualificationError("OKX soak snapshot values are invalid")
    if (
        report["event_count"] < report["complete_bar_count"] + report["funding_count"]
        or report["complete_bar_count"] < SOAK_DURATION_SECONDS // 60 - 2
        or report["funding_count"] < 1
    ):
        raise FeedQualificationError("OKX soak event totals are incomplete")
    elapsed = [snapshot["elapsed_seconds"] for snapshot in snapshots]
    event_counts = [snapshot["event_count"] for snapshot in snapshots]
    if (
        elapsed != sorted(elapsed)
        or elapsed[0] > 1
        or elapsed[-1] < SOAK_DURATION_SECONDS
        or event_counts != sorted(event_counts)
        or any(snapshot["queue_high_watermark"] > 256 for snapshot in snapshots)
        or any(
            snapshot[field] != 0
            for snapshot in snapshots
            for field in ("error_count", "rejected_count", "overflow_count")
        )
    ):
        raise FeedQualificationError("OKX soak snapshots are invalid")


def _verify_experimental_opt_in() -> list[dict[str, Any]]:
    from ib_async import IB

    from ml4t.live import AlpacaDataFeed, CryptoFeed, DataBentoFeed, IBDataFeed
    from ml4t.live.feeds.alpaca_feed import ALPACA_MISSING_GUARANTEES
    from ml4t.live.feeds.crypto_feed import CRYPTO_MISSING_GUARANTEES
    from ml4t.live.feeds.databento_feed import DATABENTO_MISSING_GUARANTEES
    from ml4t.live.feeds.experimental import ExperimentalFeedError, ExperimentalFeedWarning
    from ml4t.live.feeds.ib_feed import IB_MISSING_GUARANTEES

    constructors = {
        "AlpacaDataFeed": (
            lambda: AlpacaDataFeed("key", "secret", ["BTC/USD"]),
            lambda: AlpacaDataFeed("key", "secret", ["BTC/USD"], experimental=True),
        ),
        "IBDataFeed": (
            lambda: IBDataFeed(IB(), ["SPY"]),
            lambda: IBDataFeed(IB(), ["SPY"], experimental=True),
        ),
        "DataBentoFeed": (
            lambda: DataBentoFeed([], ["SPY"]),
            lambda: DataBentoFeed([], ["SPY"], experimental=True),
        ),
        "CryptoFeed": (
            lambda: CryptoFeed("invalid", ["BTC/USD"]),
            lambda: CryptoFeed("invalid", ["BTC/USD"], experimental=True),
        ),
    }
    missing_guarantees = {
        "AlpacaDataFeed": ALPACA_MISSING_GUARANTEES,
        "IBDataFeed": IB_MISSING_GUARANTEES,
        "DataBentoFeed": DATABENTO_MISSING_GUARANTEES,
        "CryptoFeed": CRYPTO_MISSING_GUARANTEES,
    }
    records = []
    for name, (constructor, opted_in_constructor) in constructors.items():
        try:
            constructor()
        except ExperimentalFeedError as error:
            if "experimental=True" not in str(error):
                raise FeedQualificationError(
                    f"{name} opt-in error omits the required action"
                ) from error
        else:
            raise FeedQualificationError(f"{name} constructed without experimental opt-in")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                opted_in_constructor()
            except (ImportError, ValueError):
                pass
        expected = missing_guarantees[name]
        matching = [
            warning
            for warning in caught
            if issubclass(warning.category, ExperimentalFeedWarning)
            and all(detail in str(warning.message) for detail in expected)
        ]
        if len(matching) != 1:
            raise FeedQualificationError(f"{name} does not report its missing guarantees")
        records.append(
            {
                "feed": name,
                "status": "experimental",
                "explicit_opt_in_required": True,
                "missing_guarantees": list(missing_guarantees[name]),
            }
        )
    return records


def assemble_bundle(
    candidate: dict[str, Any], report: dict[str, Any], soak_report: dict[str, Any]
) -> dict[str, Any]:
    """Combine external evidence and the installed support classification."""
    validate_okx_report(report, candidate)
    validate_soak_report(soak_report, candidate)
    bundle = {
        "schema_version": 1,
        "candidate": _candidate_identity(candidate),
        "generated_at": datetime.now(UTC).isoformat(),
        "stable_feeds": [
            {
                "feed": "OKXFundingFeed",
                "provider": "okx",
                "external_evidence": True,
                "continuous_session_seconds": soak_report["duration_seconds"],
                "reconnect_count": soak_report["reconnect_count"],
                "passed": True,
            }
        ],
        "experimental_feeds": _verify_experimental_opt_in(),
        "passed": True,
    }
    validate_feed_bundle(bundle, expected_commit=str(candidate["commit"]))
    return bundle


def validate_feed_bundle(bundle: dict[str, Any], *, expected_commit: str) -> None:
    """Validate retained feed evidence without importing provider dependencies."""
    required = {
        "schema_version",
        "candidate",
        "generated_at",
        "stable_feeds",
        "experimental_feeds",
        "passed",
    }
    if set(bundle) != required or bundle.get("schema_version") != 1:
        raise FeedQualificationError("feed bundle schema is invalid")
    candidate = bundle.get("candidate")
    if (
        not isinstance(candidate, dict)
        or candidate.get("commit") != expected_commit
        or set(candidate) != {"commit", "qualification_run_id", "version", "wheel_sha256"}
        or not _paper.COMMIT_PATTERN.fullmatch(str(candidate.get("commit", "")))
        or not isinstance(candidate.get("qualification_run_id"), int)
        or not isinstance(candidate.get("version"), str)
        or not _paper.HASH_PATTERN.fullmatch(str(candidate.get("wheel_sha256", "")))
    ):
        raise FeedQualificationError("feed bundle targets a different candidate")
    _paper._evidence_time(bundle.get("generated_at"))
    stable = bundle.get("stable_feeds")
    stable_record = stable[0] if isinstance(stable, list) and len(stable) == 1 else {}
    stable_duration = stable_record.get("continuous_session_seconds")
    if (
        set(stable_record)
        != {
            "feed",
            "provider",
            "external_evidence",
            "continuous_session_seconds",
            "reconnect_count",
            "passed",
        }
        or stable_record.get("feed") != "OKXFundingFeed"
        or stable_record.get("provider") != "okx"
        or stable_record.get("external_evidence") is not True
        or isinstance(stable_duration, bool)
        or not isinstance(stable_duration, int | float)
        or stable_duration < SOAK_DURATION_SECONDS
        or stable_record.get("reconnect_count") != 1
        or stable_record.get("passed") is not True
    ):
        raise FeedQualificationError("feed bundle does not contain exact stable OKX evidence")
    experimental = bundle.get("experimental_feeds")
    if not isinstance(experimental, list) or {
        item.get("feed") for item in experimental if isinstance(item, dict)
    } != {"AlpacaDataFeed", "IBDataFeed", "DataBentoFeed", "CryptoFeed"}:
        raise FeedQualificationError("feed bundle experimental classification is incomplete")
    for item in experimental:
        if (
            not isinstance(item, dict)
            or set(item) != {"feed", "status", "explicit_opt_in_required", "missing_guarantees"}
            or item.get("status") != "experimental"
            or item.get("explicit_opt_in_required") is not True
            or not isinstance(item.get("missing_guarantees"), list)
            or not item["missing_guarantees"]
        ):
            raise FeedQualificationError("experimental feed record is invalid")
    if bundle.get("passed") is not True:
        raise FeedQualificationError("feed bundle did not pass")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    provider = subparsers.add_parser("okx")
    provider.add_argument("--candidate", type=Path, required=True)
    provider.add_argument("--checkout-root", type=Path, required=True)
    provider.add_argument("--output", type=Path, required=True)
    soak = subparsers.add_parser("soak")
    soak.add_argument("--candidate", type=Path, required=True)
    soak.add_argument("--checkout-root", type=Path, required=True)
    soak.add_argument("--output", type=Path, required=True)
    assemble = subparsers.add_parser("assemble")
    assemble.add_argument("--candidate", type=Path, required=True)
    assemble.add_argument("--report", type=Path, required=True)
    assemble.add_argument("--soak-report", type=Path, required=True)
    assemble.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    candidate = _load_json(args.candidate)
    if args.command == "okx":
        result = asyncio.run(qualify_okx(candidate, args.checkout_root))
    elif args.command == "soak":
        result = asyncio.run(qualify_okx_soak(candidate, args.checkout_root))
    else:
        result = assemble_bundle(
            candidate,
            _load_json(args.report),
            _load_json(args.soak_report),
        )
    _write_json(args.output, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
