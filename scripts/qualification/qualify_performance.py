"""Qualify sustained LiveEngine behavior against the stable reference workloads."""

from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import importlib.metadata
import json
import os
import platform
import statistics
import time
from array import array
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import psutil
from ml4t.backtest import Strategy
from ml4t.backtest.types import Order, OrderSide, OrderStatus, OrderType
from ml4t.specs import (
    AssetTarget,
    CanonicalTargetIntent,
    EventCompletion,
    ExecutionBehavior,
    GapEvidence,
    IntentReason,
    LifecyclePhase,
    LifecycleVersion,
    MarketEvent,
    MarketEventKind,
    ResidualPolicy,
    RoundingPolicy,
    TargetMeasure,
    TradePayload,
)

from ml4t.live import LiveEngine, default_live_execution_policy
from ml4t.live.feeds.queue import BoundedEventQueue, FeedOverflowError
from ml4t.live.orders import CanonicalOrderRequest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EVENTS_PER_SECOND = 100
VIRTUAL_DURATION_SECONDS = 3_600
SUSTAINED_EVENTS = EVENTS_PER_SECOND * VIRTUAL_DURATION_SECONDS
SUSTAINED_REPETITIONS = 3
WARMUP_EVENTS = 10_000
SYMBOL_COUNT = 32
ORDER_EVENTS = 2_000
QUEUE_CAPACITY = 64
RSS_GROWTH_LIMIT_BYTES = 25 * 1024 * 1024
DISPATCH_P99_LIMIT_MS = 10.0
SHUTDOWN_LIMIT_SECONDS = 5.0
INTENT_INTERVAL = 1_000
START_TIME = datetime(2026, 1, 1, tzinfo=UTC)


def stable_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def percentile(values: array[int], proportion: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * proportion))
    return float(ordered[index])


def event_identity(index: int, asset: str, event_time: datetime, price: float) -> str:
    return f"{index}|{asset}|{event_time.isoformat()}|{price:.5f}"


def market_event(
    index: int,
    *,
    source: str,
    symbols: int = SYMBOL_COUNT,
    gap: GapEvidence | None = None,
) -> MarketEvent:
    asset = f"ASSET-{index % symbols:02d}"
    event_time = START_TIME + timedelta(milliseconds=index)
    price = 100.0 + index / 100_000
    return MarketEvent(
        version=LifecycleVersion.V1,
        event_time=event_time,
        receipt_time=event_time,
        kind=MarketEventKind.TRADE,
        completion=EventCompletion.EVOLVING,
        source=source,
        asset=asset,
        payload=TradePayload(price, 1.0),
        provider_sequence=index // symbols,
        gap=gap,
        metadata={
            "identity": event_identity(index, asset, event_time, price),
            "produced_ns": time.perf_counter_ns(),
        },
    )


class PerformanceBroker:
    """Bounded in-memory broker used to measure framework overhead."""

    def __init__(self) -> None:
        self.connected = False
        self.audit_event_count = 0
        self.submit_count = 0
        self.order_checksum = hashlib.sha256()
        self.portable_state_save_count = 0

    @property
    def is_connected(self) -> bool:
        return self.connected

    @property
    def positions(self) -> dict:
        return {}

    @property
    def pending_orders(self) -> list:
        return []

    @property
    def execution_capabilities(self) -> frozenset:
        return frozenset()

    def assert_paper_trading(self) -> None:
        """Identify the synthetic workload as safe for paper-mode checks."""

    def assert_live_trading(self) -> None:
        """Identify the synthetic workload as safe for live-mode checks."""

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def is_connected_async(self) -> bool:
        return self.connected

    async def get_positions_async(self) -> dict:
        return {}

    async def get_pending_orders_async(self, asset: str | None = None) -> list:
        return []

    async def get_position_async(self, asset: str) -> None:
        return None

    async def get_cash_async(self) -> float:
        return 100_000.0

    async def get_account_value_async(self) -> float:
        return 100_000.0

    async def submit_order_async(
        self,
        asset: str,
        quantity: float,
        side: OrderSide | None = None,
        order_type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
        stop_price: float | None = None,
        **kwargs: Any,
    ) -> Order:
        request = CanonicalOrderRequest.from_input(
            asset,
            quantity,
            side,
            order_type,
            limit_price,
            stop_price,
        )
        self.submit_count += 1
        self.order_checksum.update(
            stable_json(
                {
                    "asset": request.asset,
                    "quantity": request.quantity,
                    "side": request.side.value,
                    "order_type": request.order_type.value,
                }
            )
            + b"\n"
        )
        return Order(
            asset=request.asset,
            quantity=request.quantity,
            side=request.side,
            order_type=request.order_type,
            order_id=f"performance-{self.submit_count}",
            status=OrderStatus.PENDING,
            created_at=START_TIME,
        )

    async def cancel_order_async(self, order_id: str) -> bool:
        return False

    async def replace_order_async(self, order_id: str, **kwargs: Any) -> Order:
        raise AssertionError("replacement is outside the high-order-rate workload")

    async def close_position_async(self, asset: str) -> None:
        return None

    def record_event(self, event: str, **payload: Any) -> None:
        self.audit_event_count += 1

    def load_portable_strategy_state(self) -> dict[str, Any]:
        return {}

    def save_portable_strategy_state(self, state: dict[str, Any]) -> None:
        self.portable_state_save_count += 1


class StreamingFeed:
    def __init__(self, count: int, *, source: str = "performance") -> None:
        self.count = count
        self.source = source
        self.index = 0
        self.running = False
        self.produced = hashlib.sha256()
        self.exhausted_ns: int | None = None

    async def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False

    def __aiter__(self) -> StreamingFeed:
        return self

    async def __anext__(self) -> MarketEvent:
        if not self.running or self.index >= self.count:
            self.exhausted_ns = time.perf_counter_ns()
            raise StopAsyncIteration
        index = self.index
        self.index += 1
        event = market_event(index, source=self.source)
        self.produced.update(f"{event.metadata['identity']}\n".encode())
        return event


class SustainedStrategy(Strategy):
    def __init__(self, warmup_events: int, process: psutil.Process) -> None:
        self.warmup_events = warmup_events
        self.process = process
        self.event_count = 0
        self.consumed = hashlib.sha256()
        self.intent_checksum = hashlib.sha256()
        self.intent_count = 0
        self.latencies_ns: array[int] = array("Q")
        self.baseline_rss: int | None = None

    def on_data(self, timestamp, data, context, broker) -> None:
        self.event_count += 1
        asset = next(iter(data))
        metadata = context[asset]
        self.consumed.update(f"{metadata['identity']}\n".encode())
        if self.event_count == self.warmup_events:
            gc.collect()
            self.baseline_rss = self.process.memory_info().rss
        elif self.event_count > self.warmup_events:
            self.latencies_ns.append(time.perf_counter_ns() - metadata["produced_ns"])

        if self.event_count % INTENT_INTERVAL == 0:
            intent_index = self.event_count // INTENT_INTERVAL
            intent = CanonicalTargetIntent(
                intent_id=f"performance-target-{intent_index}",
                decision_time=timestamp,
                information_cutoff=timestamp,
                effective_session=date(2027, 1, 1) + timedelta(days=intent_index),
                effective_phase=LifecyclePhase.PRE_OPEN,
                targets=(AssetTarget(asset, TargetMeasure.QUANTITY, 1.0),),
                idempotency_key=f"performance-target-key-{intent_index}",
                measure=TargetMeasure.QUANTITY,
                cash_buffer=0.0,
                rounding=RoundingPolicy.NONE,
                residual=ResidualPolicy.KEEP_CASH,
                reason=IntentReason.REBALANCE,
            )
            accepted = broker.register_target_intent(intent)
            self.intent_checksum.update(stable_json(accepted.to_dict()) + b"\n")
            self.intent_count += 1


class CountingStrategy(Strategy):
    def __init__(self, *, delay_seconds: float = 0.0, submit_orders: bool = False) -> None:
        self.delay_seconds = delay_seconds
        self.submit_orders = submit_orders
        self.event_count = 0
        self.consumed = hashlib.sha256()

    def on_data(self, timestamp, data, context, broker) -> None:
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        index = self.event_count
        self.event_count += 1
        asset = next(iter(data))
        self.consumed.update(f"{context[asset]['identity']}\n".encode())
        if self.submit_orders:
            broker.submit_order(asset, 1.0 if index % 2 == 0 else -1.0)


class QueueFeed:
    def __init__(self, event_count: int, *, overflow: bool) -> None:
        self.event_count = event_count
        self.overflow = overflow
        self.queue = BoundedEventQueue(capacity=QUEUE_CAPACITY, feed="performance-queue")
        self.overflow_error: FeedOverflowError | None = None

    async def start(self) -> None:
        for index in range(self.event_count):
            try:
                self.queue.put_nowait(market_event(index, source="performance-queue"))
            except FeedOverflowError as error:
                self.overflow_error = error
                break
        if self.overflow_error is None:
            self.queue.finish(discard=False)

    def stop(self) -> None:
        self.queue.finish(discard=True)

    def __aiter__(self) -> QueueFeed:
        return self

    async def __anext__(self) -> MarketEvent:
        item = await self.queue.get()
        if item is None:
            raise StopAsyncIteration
        return item

    @property
    def stats(self) -> dict[str, Any]:
        return {"queue": self.queue.snapshot().to_dict()}


class ReconnectFeed:
    def __init__(self) -> None:
        self.start_count = 0
        self.stop_count = 0
        self.running = False
        self.queue: asyncio.Queue[MarketEvent | None] = asyncio.Queue()

    async def start(self) -> None:
        self.running = True
        self.queue = asyncio.Queue()
        index = min(self.start_count, 1)
        self.start_count += 1
        gap = None
        if index == 1:
            gap = GapEvidence(
                False,
                "provider sequence proves contiguous reconnect",
                previous_sequence=0,
                current_sequence=1,
            )
        await self.queue.put(market_event(index, source="reconnect", symbols=1, gap=gap))

    def stop(self) -> None:
        self.stop_count += 1
        self.running = False
        self.queue.put_nowait(None)

    def __aiter__(self) -> ReconnectFeed:
        return self

    async def __anext__(self) -> MarketEvent:
        item = await self.queue.get()
        if item is None:
            raise StopAsyncIteration
        return item


def engine_policy():
    return default_live_execution_policy(opening_auction=ExecutionBehavior.CLIENT)


async def run_idle() -> dict[str, Any]:
    broker = PerformanceBroker()
    feed = StreamingFeed(0, source="idle")
    strategy = CountingStrategy()
    engine = LiveEngine(strategy, broker, feed, execution_policy=engine_policy())
    await engine.connect()
    started = time.perf_counter()
    await engine.run()
    elapsed = time.perf_counter() - started
    return {
        "events": strategy.event_count,
        "elapsed_seconds": elapsed,
        "shutdown_seconds": elapsed,
        "runtime_state": engine.runtime_state.value,
    }


async def run_sustained(event_count: int, warmup_events: int) -> dict[str, Any]:
    process = psutil.Process()
    broker = PerformanceBroker()
    feed = StreamingFeed(event_count)
    strategy = SustainedStrategy(warmup_events, process)
    engine = LiveEngine(strategy, broker, feed, execution_policy=engine_policy())
    await engine.connect()
    started = time.perf_counter()
    await engine.run()
    elapsed = time.perf_counter() - started
    exhausted_ns = feed.exhausted_ns or time.perf_counter_ns()
    shutdown_seconds = (time.perf_counter_ns() - exhausted_ns) / 1_000_000_000
    gc.collect()
    final_rss = process.memory_info().rss
    baseline_rss = strategy.baseline_rss or final_rss
    latency_count = len(strategy.latencies_ns)
    diagnostics = engine.stats["diagnostics"]
    return {
        "events": strategy.event_count,
        "virtual_seconds": event_count / EVENTS_PER_SECOND,
        "elapsed_seconds": elapsed,
        "throughput_events_per_second": strategy.event_count / elapsed,
        "latency_sample_count": latency_count,
        "dispatch_latency_ms": {
            "p50": percentile(strategy.latencies_ns, 0.50) / 1_000_000,
            "p95": percentile(strategy.latencies_ns, 0.95) / 1_000_000,
            "p99": percentile(strategy.latencies_ns, 0.99) / 1_000_000,
        },
        "rss_baseline_bytes": baseline_rss,
        "rss_final_bytes": final_rss,
        "rss_growth_bytes": max(0, final_rss - baseline_rss),
        "shutdown_seconds": shutdown_seconds,
        "event_checksum": strategy.consumed.hexdigest(),
        "produced_event_checksum": feed.produced.hexdigest(),
        "intent_count": strategy.intent_count,
        "intent_checksum": strategy.intent_checksum.hexdigest(),
        "portable_state_save_count": broker.portable_state_save_count,
        "continuity": engine.stats["continuity"],
        "diagnostics": diagnostics,
        "audit_event_count": broker.audit_event_count,
    }


async def run_slow_strategy() -> dict[str, Any]:
    broker = PerformanceBroker()
    feed = QueueFeed(QUEUE_CAPACITY, overflow=False)
    strategy = CountingStrategy(delay_seconds=0.001)
    engine = LiveEngine(strategy, broker, feed, execution_policy=engine_policy())
    await engine.connect()
    started = time.perf_counter()
    await engine.run()
    elapsed = time.perf_counter() - started
    return {
        "events": strategy.event_count,
        "elapsed_seconds": elapsed,
        "queue": feed.stats["queue"],
        "event_checksum": strategy.consumed.hexdigest(),
    }


async def run_overload() -> dict[str, Any]:
    broker = PerformanceBroker()
    feed = QueueFeed(QUEUE_CAPACITY + 1, overflow=True)
    strategy = CountingStrategy(delay_seconds=0.001)
    engine = LiveEngine(strategy, broker, feed, execution_policy=engine_policy())
    await engine.connect()
    error: FeedOverflowError | None = None
    try:
        await engine.run()
    except FeedOverflowError as caught:
        error = caught
    safety_events = [
        event for event in engine.operational_events if event["event"] == "feed_safety_halt"
    ]
    return {
        "events_dispatched": strategy.event_count,
        "error_type": type(error).__name__ if error is not None else None,
        "error": error.to_dict() if error is not None else None,
        "queue": feed.stats["queue"],
        "safety_event_count": len(safety_events),
    }


async def run_high_order_rate(event_count: int) -> dict[str, Any]:
    broker = PerformanceBroker()
    feed = StreamingFeed(event_count, source="orders")
    strategy = CountingStrategy(submit_orders=True)
    engine = LiveEngine(strategy, broker, feed, execution_policy=engine_policy())
    await engine.connect()
    started = time.perf_counter()
    await engine.run()
    elapsed = time.perf_counter() - started

    expected = hashlib.sha256()
    for index in range(event_count):
        expected.update(
            stable_json(
                {
                    "asset": f"ASSET-{index % SYMBOL_COUNT:02d}",
                    "quantity": 1.0,
                    "side": "buy" if index % 2 == 0 else "sell",
                    "order_type": "market",
                }
            )
            + b"\n"
        )
    return {
        "events": strategy.event_count,
        "orders": broker.submit_count,
        "elapsed_seconds": elapsed,
        "orders_per_second": broker.submit_count / elapsed,
        "order_checksum": broker.order_checksum.hexdigest(),
        "expected_order_checksum": expected.hexdigest(),
        "event_checksum": strategy.consumed.hexdigest(),
        "produced_event_checksum": feed.produced.hexdigest(),
    }


async def run_reconnect() -> dict[str, Any]:
    broker = PerformanceBroker()
    feed = ReconnectFeed()
    strategy = CountingStrategy()
    engine = LiveEngine(
        strategy,
        broker,
        feed,
        feed_silence_seconds=0.02,
        watchdog_poll_seconds=0.005,
        auto_recover=True,
        recovery_cooldown_seconds=0,
        max_recovery_attempts=1,
        execution_policy=engine_policy(),
    )
    await engine.connect()

    async def stop_after_reconnect() -> None:
        while strategy.event_count < 2:
            await asyncio.sleep(0.001)
        await engine.stop()

    started = time.perf_counter()
    await asyncio.wait_for(
        asyncio.gather(engine.run(), stop_after_reconnect()),
        timeout=SHUTDOWN_LIMIT_SECONDS,
    )
    elapsed = time.perf_counter() - started
    recoveries = [
        event
        for event in engine.operational_events
        if event["event"] == "engine_recovery_succeeded"
    ]
    return {
        "events": strategy.event_count,
        "elapsed_seconds": elapsed,
        "feed_start_count": feed.start_count,
        "feed_stop_count": feed.stop_count,
        "recovery_attempts": engine.stats["recovery_attempts"],
        "recovery_event_count": len(recoveries),
        "recovery_duration_seconds": recoveries[0]["duration_seconds"] if recoveries else None,
        "continuity": engine.stats["continuity"],
        "runtime_state": engine.runtime_state.value,
    }


def dependency_versions() -> dict[str, str]:
    names = (
        "ml4t-backtest",
        "ml4t-specs",
        "ib-async",
        "alpaca-py",
        "httpx",
        "ccxt",
        "psutil",
    )
    return {name: importlib.metadata.version(name) for name in names}


def validate_report(report: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    sustained = report["workloads"]["sustained"]
    runs = sustained["runs"]
    expected_events = sustained["configuration"]["events"]
    expected_samples = expected_events - sustained["configuration"]["warmup_events"]
    event_checksums = {run["event_checksum"] for run in runs}
    intent_checksums = {run["intent_checksum"] for run in runs}
    for index, run in enumerate(runs, start=1):
        if run["events"] != expected_events:
            failures.append(f"sustained run {index} dispatched {run['events']} events")
        if run["latency_sample_count"] != expected_samples:
            failures.append(f"sustained run {index} has an incomplete latency sample")
        if run["event_checksum"] != run["produced_event_checksum"]:
            failures.append(f"sustained run {index} changed the event checksum")
        if run["throughput_events_per_second"] < EVENTS_PER_SECOND:
            failures.append(f"sustained run {index} did not sustain 100 events/second")
        if run["dispatch_latency_ms"]["p99"] >= DISPATCH_P99_LIMIT_MS:
            failures.append(f"sustained run {index} exceeded the p99 dispatch target")
        if run["rss_growth_bytes"] >= RSS_GROWTH_LIMIT_BYTES:
            failures.append(f"sustained run {index} exceeded the RSS growth target")
        if run["shutdown_seconds"] >= SHUTDOWN_LIMIT_SECONDS:
            failures.append(f"sustained run {index} exceeded the shutdown target")
        if run["continuity"]["violation_count"] != 0:
            failures.append(f"sustained run {index} reported a continuity violation")
        if run["diagnostics"]["callback_invocations_retained"] > 4_096:
            failures.append(f"sustained run {index} retained an unbounded callback trace")
        if run["diagnostics"]["operational_events_retained"] > 4_096:
            failures.append(f"sustained run {index} retained unbounded operational events")
        if run["audit_event_count"] != run["diagnostics"]["operational_events_forwarded"]:
            failures.append(f"sustained run {index} silently lost a forwarded diagnostic")
    if len(event_checksums) != 1:
        failures.append("sustained event checksums differ between repetitions")
    if len(intent_checksums) != 1:
        failures.append("sustained intent checksums differ between repetitions")

    idle = report["workloads"]["idle"]
    if idle["events"] != 0 or idle["shutdown_seconds"] >= SHUTDOWN_LIMIT_SECONDS:
        failures.append("idle workload did not stop cleanly within the shutdown target")

    slow = report["workloads"]["slow_strategy"]
    if slow["events"] != QUEUE_CAPACITY or slow["queue"]["high_watermark"] != QUEUE_CAPACITY:
        failures.append("slow strategy workload did not drain the bounded full queue")

    overload = report["workloads"]["burst_overload"]
    if (
        overload["events_dispatched"] != 0
        or overload["error_type"] != "FeedOverflowError"
        or overload["queue"]["overflow_count"] != 1
        or overload["queue"]["occupancy"] != 0
        or overload["safety_event_count"] != 1
    ):
        failures.append("burst overload was not bounded, observable, and fail-closed")

    orders = report["workloads"]["high_order_rate"]
    if (
        orders["events"] != ORDER_EVENTS
        or orders["orders"] != ORDER_EVENTS
        or orders["order_checksum"] != orders["expected_order_checksum"]
        or orders["event_checksum"] != orders["produced_event_checksum"]
    ):
        failures.append("high-order-rate workload changed or lost events or requests")

    reconnect = report["workloads"]["reconnect"]
    if (
        reconnect["events"] != 2
        or reconnect["feed_start_count"] != 2
        or reconnect["recovery_attempts"] != 1
        or reconnect["recovery_event_count"] != 1
        or reconnect["continuity"]["violation_count"] != 0
        or reconnect["runtime_state"] != "stopped"
    ):
        failures.append("reconnect workload did not recover exactly once without a gap")
    return failures


async def qualify(args: argparse.Namespace) -> dict[str, Any]:
    process = psutil.Process()
    load_before = os.getloadavg()
    sustained_runs = [
        await run_sustained(args.sustained_events, args.warmup_events)
        for _ in range(args.repetitions)
    ]
    workload = {
        "idle": await run_idle(),
        "sustained": {
            "configuration": {
                "events_per_second": EVENTS_PER_SECOND,
                "virtual_duration_seconds": args.sustained_events / EVENTS_PER_SECOND,
                "events": args.sustained_events,
                "symbols": SYMBOL_COUNT,
                "warmup_events": args.warmup_events,
                "repetitions": args.repetitions,
            },
            "summary": {
                "throughput_events_per_second_median": statistics.median(
                    run["throughput_events_per_second"] for run in sustained_runs
                ),
                "dispatch_latency_ms_p99_median": statistics.median(
                    run["dispatch_latency_ms"]["p99"] for run in sustained_runs
                ),
                "dispatch_latency_ms_p99_range": [
                    min(run["dispatch_latency_ms"]["p99"] for run in sustained_runs),
                    max(run["dispatch_latency_ms"]["p99"] for run in sustained_runs),
                ],
                "rss_growth_bytes_range": [
                    min(run["rss_growth_bytes"] for run in sustained_runs),
                    max(run["rss_growth_bytes"] for run in sustained_runs),
                ],
                "shutdown_seconds_range": [
                    min(run["shutdown_seconds"] for run in sustained_runs),
                    max(run["shutdown_seconds"] for run in sustained_runs),
                ],
            },
            "runs": sustained_runs,
        },
        "slow_strategy": await run_slow_strategy(),
        "burst_overload": await run_overload(),
        "high_order_rate": await run_high_order_rate(ORDER_EVENTS),
        "reconnect": await run_reconnect(),
    }
    report: dict[str, Any] = {
        "schema_version": 1,
        "candidate": {
            "revision": candidate_revision(),
        },
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
            "process_cpu_affinity": process.cpu_affinity(),
            "host_memory_bytes": psutil.virtual_memory().total,
            "load_average_before": load_before,
            "load_average_after": os.getloadavg(),
            "dependencies": dependency_versions(),
        },
        "targets": {
            "rss_growth_bytes_less_than": RSS_GROWTH_LIMIT_BYTES,
            "dispatch_latency_ms_p99_less_than": DISPATCH_P99_LIMIT_MS,
            "shutdown_seconds_less_than": SHUTDOWN_LIMIT_SECONDS,
            "minimum_throughput_events_per_second": EVENTS_PER_SECOND,
            "queue_capacity": QUEUE_CAPACITY,
        },
        "workloads": workload,
    }
    report["failures"] = validate_report(report)
    report["passed"] = not report["failures"]
    return report


def git_revision() -> str:
    import subprocess

    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def candidate_revision() -> str:
    return os.environ.get("CANDIDATE_SHA") or git_revision()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--sustained-events", type=int, default=SUSTAINED_EVENTS)
    parser.add_argument("--repetitions", type=int, default=SUSTAINED_REPETITIONS)
    parser.add_argument("--warmup-events", type=int, default=WARMUP_EVENTS)
    args = parser.parse_args()
    if args.sustained_events <= 0:
        parser.error("--sustained-events must be positive")
    if args.repetitions <= 0:
        parser.error("--repetitions must be positive")
    if not 0 < args.warmup_events < args.sustained_events:
        parser.error("--warmup-events must be positive and less than --sustained-events")
    return args


def main() -> int:
    args = parse_args()
    report = asyncio.run(qualify(args))
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered, end="")
    print(f"performance qualification: {'PASS' if report['passed'] else 'FAIL'}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
