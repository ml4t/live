"""Sustained LiveEngine dispatcher and diagnostic-retention checks."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from ml4t.backtest import Strategy
from ml4t.specs import (
    EventCompletion,
    LifecycleVersion,
    MarketEvent,
    MarketEventKind,
    TradePayload,
)

from ml4t.live import LiveEngine

pytestmark = pytest.mark.stress

EVENT_COUNT = 50_000
SYMBOL_COUNT = 32


class AuditBroker:
    """Minimal broker that counts forwarded diagnostics without retaining payloads."""

    def __init__(self) -> None:
        self.connected = False
        self.audit_event_count = 0

    @property
    def is_connected(self) -> bool:
        return self.connected

    @property
    def positions(self) -> dict:
        return {}

    @property
    def pending_orders(self) -> list:
        return []

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    def record_event(self, event: str, **payload: Any) -> None:
        self.audit_event_count += 1


class SustainedFeed:
    def __init__(self, count: int) -> None:
        self.count = count
        self.index = 0
        self.running = False
        self.produced = hashlib.sha256()
        self.started_at = datetime(2026, 1, 1, tzinfo=UTC)

    async def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False

    def __aiter__(self) -> SustainedFeed:
        return self

    async def __anext__(self) -> MarketEvent:
        if not self.running or self.index >= self.count:
            raise StopAsyncIteration
        index = self.index
        self.index += 1
        asset = f"ASSET-{index % SYMBOL_COUNT:02d}"
        event_time = self.started_at + timedelta(milliseconds=index)
        price = 100.0 + index / 100_000
        identity = f"{index}|{asset}|{event_time.isoformat()}|{price:.5f}\n".encode()
        self.produced.update(identity)
        return MarketEvent(
            version=LifecycleVersion.V1,
            event_time=event_time,
            receipt_time=event_time,
            kind=MarketEventKind.TRADE,
            completion=EventCompletion.EVOLVING,
            source="sustained",
            asset=asset,
            payload=TradePayload(price, 1.0),
            provider_sequence=index // SYMBOL_COUNT,
            metadata={"identity": identity.decode().rstrip()},
        )


class ChecksumStrategy(Strategy):
    def __init__(self) -> None:
        self.event_count = 0
        self.consumed = hashlib.sha256()

    def on_data(self, timestamp, data, context, broker) -> None:
        self.event_count += 1
        asset = next(iter(data))
        self.consumed.update(f"{context[asset]['identity']}\n".encode())


@pytest.mark.asyncio
async def test_sustained_engine_retains_bounded_diagnostics_without_silent_loss() -> None:
    broker = AuditBroker()
    feed = SustainedFeed(EVENT_COUNT)
    strategy = ChecksumStrategy()
    engine = LiveEngine(strategy, broker, feed)

    await engine.connect()
    await engine.run()

    diagnostics = engine.stats["diagnostics"]
    assert strategy.event_count == EVENT_COUNT
    assert engine.stats["event_count"] == EVENT_COUNT
    assert feed.produced.hexdigest() == strategy.consumed.hexdigest()
    assert engine.stats["continuity"] == {
        "generation": 0,
        "tracked_streams": SYMBOL_COUNT,
        "accepted_count": EVENT_COUNT,
        "duplicate_count": 0,
        "violation_count": 0,
        "last_sequences": {
            f"sustained:ASSET-{symbol:02d}:trade": (EVENT_COUNT - 1 - symbol) // SYMBOL_COUNT
            for symbol in range(SYMBOL_COUNT)
        },
    }
    assert diagnostics["callback_invocations_total"] == EVENT_COUNT + 3
    assert diagnostics["callback_invocations_retained"] <= 4_096
    assert diagnostics["callback_invocations_dropped"] > 0
    assert diagnostics["operational_events_retained"] <= 4_096
    assert diagnostics["operational_events_dropped"] > 0
    assert broker.audit_event_count == diagnostics["operational_events_forwarded"]
    assert diagnostics["operational_events_total"] - broker.audit_event_count == (
        2 * diagnostics["callback_invocations_total"] + 3
    )
