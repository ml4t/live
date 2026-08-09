from __future__ import annotations

import asyncio
import json
import signal
import threading
import time
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import pytest
from ml4t.backtest import Strategy

from ml4t.live import (
    LiveEngine,
    LiveRiskConfig,
    RuntimeCleanupError,
    RuntimeFailureError,
    RuntimeState,
    SafeBroker,
    runtime_error_context,
)
from ml4t.live.persistence import SecureStateStore


class FaultBroker:
    def __init__(
        self,
        *,
        fail_connect_calls: set[int] | None = None,
        fail_disconnect: bool = False,
        fail_record: bool = False,
    ) -> None:
        self.connected = False
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.fail_connect_calls = fail_connect_calls or set()
        self.fail_disconnect = fail_disconnect
        self.fail_record = fail_record
        self.events: list[tuple[str, dict[str, Any]]] = []

    @property
    def is_connected(self) -> bool:
        return self.connected

    @property
    def positions(self) -> dict:
        return {}

    @property
    def pending_orders(self) -> list:
        return []

    def record_event(self, event: str, **payload: Any) -> None:
        if self.fail_record:
            raise RuntimeError("operational record failed")
        self.events.append((event, payload))

    def assert_paper_trading(self) -> None:
        """Identify this deterministic adapter as a paper venue."""

    def assert_live_trading(self) -> None:
        """Allow tests that explicitly exercise the live routing contract."""

    async def connect(self) -> None:
        self.connect_calls += 1
        self.connected = True
        if self.connect_calls in self.fail_connect_calls:
            raise RuntimeError(f"connect failure {self.connect_calls}")

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.connected = False
        if self.fail_disconnect:
            raise RuntimeError("disconnect failure")

    async def is_connected_async(self) -> bool:
        return self.connected

    async def get_positions_async(self) -> dict:
        return {}

    async def get_pending_orders_async(self, asset: str | None = None) -> list:
        return []


class FaultFeed:
    def __init__(
        self,
        *,
        fail_start_calls: set[int] | None = None,
        fail_stop: bool = False,
        fail_iteration: bool = False,
        block: bool = False,
    ) -> None:
        self.fail_start_calls = fail_start_calls or set()
        self.fail_stop = fail_stop
        self.fail_iteration = fail_iteration
        self.block = block
        self.start_calls = 0
        self.stop_calls = 0
        self.close_calls = 0
        self.started = False
        self._iteration_failed = False
        self._queue: asyncio.Queue[None] = asyncio.Queue()

    async def start(self) -> None:
        self.start_calls += 1
        self.started = True
        self._queue = asyncio.Queue()
        if self.start_calls in self.fail_start_calls:
            raise RuntimeError(f"feed start failure {self.start_calls}")

    def stop(self) -> None:
        self.stop_calls += 1
        self.started = False
        self._queue.put_nowait(None)
        if self.fail_stop:
            raise RuntimeError("feed stop failure")

    def __aiter__(self) -> AsyncIterator[tuple[datetime, dict, dict]]:
        return self

    async def close(self) -> None:
        self.close_calls += 1

    async def __anext__(self) -> tuple[datetime, dict, dict]:
        if self.fail_iteration and not self._iteration_failed:
            self._iteration_failed = True
            raise RuntimeError("feed iteration failure")
        if self.block:
            await self._queue.get()
        raise StopAsyncIteration


class PhaseStrategy(Strategy):
    def __init__(self, fail_phase: str | None = None) -> None:
        self.fail_phase = fail_phase
        self.start_calls = 0
        self.prepare_calls = 0
        self.data_calls = 0
        self.end_calls = 0

    def on_start(self, broker: Any) -> None:
        self.start_calls += 1
        if self.fail_phase == "start":
            raise RuntimeError("strategy start failure")

    def on_prepare(self, broker: Any, config: Any | None = None) -> None:
        self.prepare_calls += 1
        if self.fail_phase == "prepare":
            raise RuntimeError("strategy prepare failure")

    def on_data(self, timestamp: datetime, data: dict, context: dict, broker: Any) -> None:
        self.data_calls += 1

    def on_end(self, broker: Any) -> None:
        self.end_calls += 1
        if self.fail_phase == "end":
            raise RuntimeError("strategy end failure")


async def wait_for_state(engine: LiveEngine, state: RuntimeState) -> None:
    for _ in range(1_000):
        if engine.runtime_state is state:
            return
        await asyncio.sleep(0.001)
    raise AssertionError(f"engine did not reach {state.value}")


@pytest.mark.parametrize(
    "runtime_options",
    [
        {"feed_silence_seconds": 0},
        {"watchdog_poll_seconds": 0},
        {"recovery_cooldown_seconds": float("nan")},
        {"max_recovery_attempts": -1},
        {"max_recovery_attempts": True},
    ],
)
def test_invalid_runtime_configuration_precedes_side_effects(runtime_options) -> None:
    broker = FaultBroker()
    feed = FaultFeed()

    with pytest.raises((TypeError, ValueError)):
        LiveEngine(PhaseStrategy(), broker, feed, **runtime_options)

    assert broker.connect_calls == 0
    assert feed.start_calls == 0


@pytest.mark.asyncio
async def test_preflight_record_failure_has_no_runtime_side_effects() -> None:
    broker = FaultBroker(fail_record=True)
    feed = FaultFeed()
    engine = LiveEngine(PhaseStrategy(), broker, feed)

    with pytest.raises(RuntimeError, match="operational record failed"):
        await engine.connect()

    assert broker.connect_calls == 0
    assert broker.disconnect_calls == 0
    assert feed.start_calls == 0
    assert feed.stop_calls == 0
    assert engine.runtime_state is RuntimeState.FAILED


@pytest.mark.asyncio
async def test_partial_broker_connect_failure_is_rolled_back_once() -> None:
    broker = FaultBroker(fail_connect_calls={1})
    feed = FaultFeed()
    engine = LiveEngine(PhaseStrategy(), broker, feed)

    with pytest.raises(RuntimeError, match="connect failure"):
        await engine.connect()

    assert broker.connected is False
    assert broker.disconnect_calls == 1
    assert feed.start_calls == 0
    assert feed.stop_calls == 0
    assert engine.runtime_state is RuntimeState.FAILED


@pytest.mark.asyncio
async def test_failed_safe_broker_connect_releases_persistence_writer(tmp_path) -> None:
    raw_broker = FaultBroker(fail_connect_calls={1})
    state_path = tmp_path / "state.json"
    safe_broker = SafeBroker(
        raw_broker,
        LiveRiskConfig(
            execution_mode="paper",
            state_file=str(state_path),
            journal_file=str(tmp_path / "journal.jsonl"),
            max_data_staleness_seconds=None,
            max_daily_loss=None,
            max_drawdown_pct=None,
        ),
    )
    engine = LiveEngine(PhaseStrategy(), safe_broker, FaultFeed())

    with pytest.raises(RuntimeError, match="connect failure"):
        await engine.connect()

    assert raw_broker.disconnect_calls == 1
    probe = SecureStateStore(state_path)
    probe.acquire_writer()
    probe.release_writer()
    assert engine.runtime_state is RuntimeState.FAILED


@pytest.mark.asyncio
async def test_completed_safe_broker_run_releases_persistence_writer(tmp_path) -> None:
    raw_broker = FaultBroker()
    state_path = tmp_path / "state.json"
    safe_broker = SafeBroker(
        raw_broker,
        LiveRiskConfig(
            execution_mode="paper",
            state_file=str(state_path),
            journal_file=str(tmp_path / "journal.jsonl"),
            max_data_staleness_seconds=None,
            max_daily_loss=None,
            max_drawdown_pct=None,
        ),
    )
    engine = LiveEngine(PhaseStrategy(), safe_broker, FaultFeed())

    await engine.connect()
    await engine.run()

    probe = SecureStateStore(state_path)
    probe.acquire_writer()
    probe.release_writer()
    assert raw_broker.disconnect_calls == 1
    assert engine.runtime_state is RuntimeState.STOPPED


@pytest.mark.asyncio
async def test_terminal_run_releases_every_owned_resource_within_five_seconds(tmp_path) -> None:
    raw_broker = FaultBroker()
    feed = FaultFeed()
    state_path = tmp_path / "state.json"
    safe_broker = SafeBroker(
        raw_broker,
        LiveRiskConfig(
            execution_mode="paper",
            state_file=str(state_path),
            journal_file=str(tmp_path / "journal.jsonl"),
            max_data_staleness_seconds=None,
            max_daily_loss=None,
            max_drawdown_pct=None,
        ),
    )
    engine = LiveEngine(PhaseStrategy(), safe_broker, feed)

    started = time.monotonic()
    await engine.connect()
    await engine.run()
    elapsed = time.monotonic() - started

    owned_tasks = {"ml4t-live-watchdog", "ml4t-live-signal-shutdown"}
    assert elapsed < 5
    assert raw_broker.connected is False
    assert raw_broker.disconnect_calls == 1
    assert feed.stop_calls == 1
    assert feed.close_calls == 1
    assert not [task for task in asyncio.all_tasks() if task.get_name() in owned_tasks]
    assert not [
        thread for thread in threading.enumerate() if thread.name.startswith("ml4t-live-strategy")
    ]
    probe = SecureStateStore(state_path)
    probe.acquire_writer()
    probe.release_writer()


@pytest.mark.asyncio
async def test_partial_feed_start_failure_rolls_back_feed_then_broker() -> None:
    broker = FaultBroker()
    feed = FaultFeed(fail_start_calls={1})
    engine = LiveEngine(PhaseStrategy(), broker, feed)

    with pytest.raises(RuntimeError, match="feed start failure") as captured:
        await engine.connect()

    assert feed.stop_calls == 1
    assert broker.disconnect_calls == 1
    assert broker.connected is False
    assert engine.runtime_state is RuntimeState.FAILED
    assert engine.stats["last_cleanup_result"]["feed"] == "released"
    assert engine.stats["last_cleanup_result"]["broker"] == "released"
    assert [transition.current for transition in engine.runtime_transitions] == [
        RuntimeState.PREFLIGHT,
        RuntimeState.CONNECTING_BROKER,
        RuntimeState.RECONCILING,
        RuntimeState.STARTING_FEED,
        RuntimeState.STOPPING,
        RuntimeState.FAILED,
    ]
    assert runtime_error_context(captured.value).to_dict() == {
        "component": "feed",
        "operation": "start",
        "runtime_state": "starting_feed",
        "recovery_action": "correct the feed failure, then call connect() again",
        "root_cause_type": "RuntimeError",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["start", "prepare"])
async def test_strategy_startup_failure_runs_end_once_and_releases_runtime(phase: str) -> None:
    strategy = PhaseStrategy(fail_phase=phase)
    broker = FaultBroker()
    feed = FaultFeed()
    engine = LiveEngine(strategy, broker, feed)
    await engine.connect()

    with pytest.raises(RuntimeError, match=f"strategy {phase} failure") as captured:
        await engine.run()

    assert strategy.start_calls == 1
    assert strategy.prepare_calls == (1 if phase == "prepare" else 0)
    assert strategy.end_calls == 1
    assert feed.stop_calls == 1
    assert broker.disconnect_calls == 1
    assert engine.runtime_state is RuntimeState.FAILED
    context = runtime_error_context(captured.value)
    assert context.component == "strategy"
    assert context.operation == f"on_{phase}"
    assert context.runtime_state is RuntimeState.STARTING_STRATEGY
    assert context.recovery_action == "correct the strategy callback before restarting"
    assert context.root_cause_type == "RuntimeError"


@pytest.mark.asyncio
async def test_feed_exception_fails_and_finalizes_the_complete_lifecycle() -> None:
    strategy = PhaseStrategy()
    broker = FaultBroker()
    feed = FaultFeed(fail_iteration=True)
    engine = LiveEngine(strategy, broker, feed)
    await engine.connect()

    with pytest.raises(RuntimeError, match="feed iteration failure") as captured:
        await engine.run()

    assert (strategy.start_calls, strategy.prepare_calls, strategy.end_calls) == (1, 1, 1)
    assert feed.stop_calls == 1
    assert broker.disconnect_calls == 1
    assert engine.runtime_state is RuntimeState.FAILED
    context = runtime_error_context(captured.value)
    assert (context.component, context.operation) == ("feed", "read")
    assert context.runtime_state is RuntimeState.RUNNING
    assert context.recovery_action == "restore the feed and establish continuity before restarting"


@pytest.mark.asyncio
async def test_runtime_error_context_and_diagnostics_redact_sensitive_failure_text() -> None:
    secret = "PKSUPERSECRET123"
    account = "DU7654321"

    class SensitiveStrategy(PhaseStrategy):
        def on_start(self, broker: Any) -> None:
            raise RuntimeError(f"api_key={secret} account={account} Bearer hidden-token")

    engine = LiveEngine(SensitiveStrategy(), FaultBroker(), FaultFeed())
    await engine.connect()

    with pytest.raises(RuntimeError) as captured:
        await engine.run()

    retained = (
        json.dumps(engine.operational_events, default=str)
        + str(captured.value)
        + str(captured.value.__notes__)
    )
    assert secret not in retained
    assert account not in retained
    assert "hidden-token" not in retained
    assert retained.count("[REDACTED]") >= 3
    assert runtime_error_context(captured.value).component == "strategy"


@pytest.mark.asyncio
async def test_end_failure_does_not_prevent_resource_cleanup() -> None:
    strategy = PhaseStrategy(fail_phase="end")
    broker = FaultBroker()
    feed = FaultFeed()
    engine = LiveEngine(strategy, broker, feed)
    await engine.connect()

    with pytest.raises(RuntimeError, match="strategy end failure"):
        await engine.run()

    assert strategy.end_calls == 1
    assert feed.stop_calls == 1
    assert broker.disconnect_calls == 1
    assert engine.runtime_state is RuntimeState.FAILED


@pytest.mark.asyncio
async def test_health_callback_failure_still_runs_end_and_cleanup() -> None:
    strategy = PhaseStrategy()
    broker = FaultBroker()
    feed = FaultFeed(block=True)

    def fail_health_callback(health: str, status: dict[str, Any]) -> None:
        raise RuntimeError(f"health callback failure: {health}")

    engine = LiveEngine(
        strategy,
        broker,
        feed,
        watchdog_poll_seconds=0.001,
        on_health_change=fail_health_callback,
    )
    await engine.connect()

    with pytest.raises(RuntimeError, match="health callback failure"):
        await asyncio.wait_for(engine.run(), timeout=1)

    assert strategy.end_calls == 1
    assert feed.stop_calls == 1
    assert broker.disconnect_calls == 1
    assert engine.runtime_state is RuntimeState.FAILED


@pytest.mark.asyncio
async def test_repeated_concurrent_stop_releases_each_resource_once() -> None:
    broker = FaultBroker()
    feed = FaultFeed(block=True)
    engine = LiveEngine(PhaseStrategy(), broker, feed)
    await engine.connect()

    await asyncio.gather(engine.stop(), engine.stop(), engine.stop())

    assert feed.stop_calls == 1
    assert broker.disconnect_calls == 1
    assert engine.runtime_state is RuntimeState.STOPPED
    assert not [task for task in asyncio.all_tasks() if task.get_name() == "ml4t-live-watchdog"]


@pytest.mark.asyncio
async def test_shutdown_signal_uses_transactional_stop_without_task_leaks() -> None:
    strategy = PhaseStrategy()
    broker = FaultBroker()
    feed = FaultFeed(block=True)
    engine = LiveEngine(strategy, broker, feed)
    await engine.connect()
    run_task = asyncio.create_task(engine.run())
    await wait_for_state(engine, RuntimeState.RUNNING)

    engine._request_signal_shutdown(signal.SIGTERM)
    await asyncio.wait_for(run_task, timeout=1)
    await asyncio.sleep(0)

    assert strategy.end_calls == 1
    assert feed.stop_calls == 1
    assert broker.disconnect_calls == 1
    assert engine.runtime_state is RuntimeState.STOPPED
    assert engine.stats["last_cleanup_result"]["signals"] == "released"
    assert not [
        task
        for task in asyncio.all_tasks()
        if task.get_name() in {"ml4t-live-watchdog", "ml4t-live-signal-shutdown"}
    ]


@pytest.mark.asyncio
async def test_reconnect_runs_each_boundary_once_per_run_and_retains_counts() -> None:
    strategy = PhaseStrategy()
    broker = FaultBroker()
    feed = FaultFeed()
    engine = LiveEngine(strategy, broker, feed)

    for _ in range(2):
        await engine.connect()
        await engine.run()

    assert (strategy.start_calls, strategy.prepare_calls, strategy.end_calls) == (2, 2, 2)
    assert broker.connect_calls == 2
    assert broker.disconnect_calls == 2
    assert feed.start_calls == 2
    assert feed.stop_calls == 2
    assert engine.stats["callback_counts"]["run_start"] == 2
    assert engine.stats["callback_counts"]["causal_initialization"] == 2
    assert engine.stats["callback_counts"]["run_end"] == 2
    assert engine.runtime_state is RuntimeState.STOPPED


@pytest.mark.asyncio
async def test_cleanup_failure_is_observable_and_terminal() -> None:
    broker = FaultBroker()
    feed = FaultFeed(fail_stop=True)
    engine = LiveEngine(PhaseStrategy(), broker, feed)
    await engine.connect()

    with pytest.raises(RuntimeCleanupError) as captured:
        await engine.stop()

    assert captured.value.cleanup_result["feed"] == "failed:RuntimeError"
    assert runtime_error_context(captured.value).to_dict() == {
        "component": "runtime_resources",
        "operation": "release",
        "runtime_state": "failed",
        "recovery_action": "correct the reported release failure, then call stop() again",
        "root_cause_type": "RuntimeCleanupError",
    }
    assert broker.disconnect_calls == 1
    assert engine.runtime_state is RuntimeState.FAILED


@pytest.mark.asyncio
async def test_recovery_exhaustion_is_bounded_without_repeating_callbacks() -> None:
    strategy = PhaseStrategy()
    broker = FaultBroker(fail_connect_calls={2, 3})
    feed = FaultFeed(block=True)
    engine = LiveEngine(
        strategy,
        broker,
        feed,
        watchdog_poll_seconds=0.001,
        auto_recover=True,
        recovery_cooldown_seconds=0,
        max_recovery_attempts=2,
    )
    await engine.connect()
    run_task = asyncio.create_task(engine.run())
    await wait_for_state(engine, RuntimeState.RUNNING)
    broker.connected = False

    with pytest.raises(RuntimeFailureError, match="recovery_exhausted") as captured:
        await asyncio.wait_for(run_task, timeout=1)

    assert broker.connect_calls == 3
    assert engine.stats["recovery_attempts"] == 2
    assert (strategy.start_calls, strategy.prepare_calls, strategy.end_calls) == (1, 1, 1)
    assert engine.runtime_state is RuntimeState.FAILED
    attempts = [
        event for event in engine.operational_events if event["event"] == "engine_recovery_failed"
    ]
    assert [event["attempt"] for event in attempts] == [1, 2]
    assert all(event["duration_seconds"] >= 0 for event in attempts)
    assert all(event["last_known_sequence"] == 0 for event in attempts)
    assert all("cleanup_result" in event for event in attempts)
    assert not [task for task in asyncio.all_tasks() if task.get_name() == "ml4t-live-watchdog"]
    assert runtime_error_context(captured.value).to_dict() == {
        "component": "engine",
        "operation": "recover",
        "runtime_state": "failed",
        "recovery_action": (
            "inspect recovery events and restore the failed dependency before restart"
        ),
        "root_cause_type": "RuntimeFailureError",
    }
