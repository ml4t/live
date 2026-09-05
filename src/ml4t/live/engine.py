"""LiveEngine - Async orchestration layer for live trading.

Bridges async infrastructure (brokers, data feeds) with synchronous Strategy.on_data().

Key Design:
1. Strategy lifecycle runs on one dedicated worker thread
2. ThreadSafeBrokerWrapper passed to strategy for sync broker calls
3. Graceful shutdown on SIGINT/SIGTERM
4. Configurable error handling and watchdog-based recovery

Thread Model:
- Main thread: asyncio event loop (broker I/O, data feed)
- Worker thread: all synchronous strategy lifecycle callbacks
- Communication: run_coroutine_threadsafe() via ThreadSafeBrokerWrapper

Example:
    engine = LiveEngine(strategy, broker, feed)
    await engine.connect()

    try:
        await engine.run()
    except KeyboardInterrupt:
        await engine.stop()
"""

import asyncio
import inspect
import logging
import math
import signal
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from enum import StrEnum
from time import monotonic
from typing import Any, cast
from zoneinfo import ZoneInfo

from ml4t.backtest import BacktestConfig, Strategy
from ml4t.specs import (
    LIFECYCLE_V1,
    EventCompletion,
    ExecutionPolicy,
    HistoricalStrategyCompatibilityError,
    LifecyclePhase,
    LifecycleVersion,
    MarketEvent,
    MarketEventKind,
    negotiate_lifecycle_version,
    require_historical_strategy_compatibility,
)

from .feeds.events import (
    ContinuityDisposition,
    EventContinuityTracker,
    FeedContinuityError,
    FeedContractError,
    strategy_input,
    utc_datetime,
    validate_event_timing,
)
from .feeds.queue import FeedOverflowError
from .lifecycle import LiveLifecycleDispatcher
from .persistence import redact_sensitive
from .protocols import AsyncBrokerProtocol, DataFeedProtocol
from .runtime import LiveStrategyRuntime, default_live_execution_policy
from .wrappers import ThreadSafeBrokerWrapper

logger = logging.getLogger(__name__)

US_EASTERN = ZoneInfo("America/New_York")
US_EQUITY_OPEN = dt_time(9, 30)
US_EQUITY_CLOSE = dt_time(16, 0)
RECOVERABLE_HEALTH_STATES = {"feed_silent", "broker_disconnected"}
RETAINED_OPERATIONAL_EVENT_LIMIT = 4_096


class RuntimeState(StrEnum):
    """One explicit phase of engine resource and strategy ownership."""

    STOPPED = "stopped"
    PREFLIGHT = "preflight"
    CONNECTING_BROKER = "connecting_broker"
    RECONCILING = "reconciling"
    STARTING_FEED = "starting_feed"
    READY = "ready"
    STARTING_STRATEGY = "starting_strategy"
    RUNNING = "running"
    DEGRADED = "degraded"
    RECOVERING = "recovering"
    STOPPING = "stopping"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RuntimeErrorContext:
    """Redacted operator context attached to a runtime exception."""

    component: str
    operation: str
    runtime_state: RuntimeState
    recovery_action: str
    root_cause_type: str

    def to_dict(self) -> dict[str, str]:
        """Return machine-readable context without exception text."""
        return {
            "component": self.component,
            "operation": self.operation,
            "runtime_state": self.runtime_state.value,
            "recovery_action": self.recovery_action,
            "root_cause_type": self.root_cause_type,
        }


def runtime_error_context(error: BaseException) -> RuntimeErrorContext | None:
    """Return structured runtime context when the engine attached it."""
    context = getattr(error, "runtime_context", None)
    return context if isinstance(context, RuntimeErrorContext) else None


def _attach_runtime_error_context(
    error: BaseException,
    *,
    component: str,
    operation: str,
    runtime_state: RuntimeState,
    recovery_action: str,
) -> None:
    message = str(error)
    redacted_message = str(redact_sensitive(message))
    if redacted_message != message:
        try:
            error.args = (redacted_message,)
        except (AttributeError, TypeError):
            pass
    if runtime_error_context(error) is not None:
        return
    context = RuntimeErrorContext(
        component=component,
        operation=operation,
        runtime_state=runtime_state,
        recovery_action=recovery_action,
        root_cause_type=type(error).__name__,
    )
    cast(Any, error).runtime_context = context
    error.add_note(
        "runtime context: "
        f"component={component}; operation={operation}; state={runtime_state.value}; "
        f"recovery={recovery_action}"
    )


@dataclass(frozen=True, slots=True)
class RuntimeTransition:
    """Structured evidence for one runtime state change."""

    occurred_at: datetime
    previous: RuntimeState
    current: RuntimeState
    reason: str
    attempt: int | None
    last_known_sequence: int


class RuntimeCleanupError(RuntimeError):
    """Raised when runtime finalization cannot release every acquired resource."""

    def __init__(self, cleanup_result: dict[str, str]) -> None:
        self.cleanup_result = dict(cleanup_result)
        super().__init__(f"runtime cleanup failed: {self.cleanup_result}")
        _attach_runtime_error_context(
            self,
            component="runtime_resources",
            operation="release",
            runtime_state=RuntimeState.FAILED,
            recovery_action="correct the reported release failure, then call stop() again",
        )


class RuntimeFailureError(RuntimeError):
    """Raised when an asynchronous runtime failure reaches a terminal state."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)
        _attach_runtime_error_context(
            self,
            component="engine",
            operation="recover",
            runtime_state=RuntimeState.FAILED,
            recovery_action="inspect recovery events and restore the failed dependency before restart",
        )


_ALLOWED_RUNTIME_TRANSITIONS: dict[RuntimeState, set[RuntimeState]] = {
    RuntimeState.STOPPED: {RuntimeState.PREFLIGHT},
    RuntimeState.PREFLIGHT: {
        RuntimeState.CONNECTING_BROKER,
        RuntimeState.STOPPING,
        RuntimeState.FAILED,
    },
    RuntimeState.CONNECTING_BROKER: {
        RuntimeState.RECONCILING,
        RuntimeState.DEGRADED,
        RuntimeState.STOPPING,
        RuntimeState.FAILED,
    },
    RuntimeState.RECONCILING: {
        RuntimeState.STARTING_FEED,
        RuntimeState.DEGRADED,
        RuntimeState.STOPPING,
        RuntimeState.FAILED,
    },
    RuntimeState.STARTING_FEED: {
        RuntimeState.READY,
        RuntimeState.RUNNING,
        RuntimeState.DEGRADED,
        RuntimeState.STOPPING,
        RuntimeState.FAILED,
    },
    RuntimeState.READY: {
        RuntimeState.STARTING_STRATEGY,
        RuntimeState.STOPPING,
        RuntimeState.FAILED,
    },
    RuntimeState.STARTING_STRATEGY: {
        RuntimeState.RUNNING,
        RuntimeState.STOPPING,
        RuntimeState.FAILED,
    },
    RuntimeState.RUNNING: {
        RuntimeState.DEGRADED,
        RuntimeState.STOPPING,
        RuntimeState.FAILED,
    },
    RuntimeState.DEGRADED: {
        RuntimeState.RECOVERING,
        RuntimeState.STOPPING,
        RuntimeState.FAILED,
    },
    RuntimeState.RECOVERING: {
        RuntimeState.CONNECTING_BROKER,
        RuntimeState.DEGRADED,
        RuntimeState.STOPPING,
        RuntimeState.FAILED,
    },
    RuntimeState.STOPPING: {RuntimeState.STOPPED, RuntimeState.FAILED},
    RuntimeState.FAILED: {RuntimeState.PREFLIGHT, RuntimeState.STOPPING, RuntimeState.STOPPED},
}


class LiveEngine:
    """Async live trading engine.

    Bridges async infrastructure with sync Strategy.on_data().
    """

    def __init__(
        self,
        strategy: Strategy,
        broker: AsyncBrokerProtocol,
        feed: DataFeedProtocol,
        *,
        on_error: Callable[[Exception, datetime, dict], None] | None = None,
        feed_silence_seconds: float | None = None,
        watchdog_poll_seconds: float = 1.0,
        halt_on_unhealthy: bool = False,
        auto_recover: bool = False,
        recovery_cooldown_seconds: float = 5.0,
        max_recovery_attempts: int = 3,
        max_event_age_seconds: float | None = None,
        on_health_change: Callable[[str, dict[str, Any]], None] | None = None,
        strategy_callback_timeout_seconds: float = 5.0,
        lifecycle_version: LifecycleVersion | str = LifecycleVersion.V1,
        execution_policy: ExecutionPolicy | None = None,
        strategy_config: BacktestConfig | None = None,
    ) -> None:
        """Initialize LiveEngine.

        Args:
            strategy: Strategy instance to execute.
            broker: Async broker implementation.
            feed: Data feed providing timestamp, data, context tuples.
            on_error: Custom error handler callback.
            feed_silence_seconds: Optional threshold for degraded feed reporting.
            watchdog_poll_seconds: Poll interval for runtime health monitoring.
            halt_on_unhealthy: Stop the engine when watchdog detects a degraded state.
            auto_recover: Attempt reconnect/restart when watchdog detects a recoverable state.
            recovery_cooldown_seconds: Delay between recovery attempts.
            max_recovery_attempts: Maximum recovery attempts before stopping.
            max_event_age_seconds: Maximum provider-event age before dispatch. When omitted, use
                the supported feed's declared limit if present.
            on_health_change: Optional callback invoked when runtime health changes.
            strategy_callback_timeout_seconds: Maximum callback duration. A callback that exceeds
                this duration is allowed to become quiescent before a typed timeout aborts the run.
            lifecycle_version: Portable strategy lifecycle version.
            execution_policy: Explicit live execution capabilities and behavior.
            strategy_config: Backtest strategy configuration supplied to ``on_prepare``.
        """
        negotiated_version = negotiate_lifecycle_version(lifecycle_version)
        self._validate_strategy_lifecycle(strategy)
        self._validate_runtime_configuration(
            feed_silence_seconds=feed_silence_seconds,
            watchdog_poll_seconds=watchdog_poll_seconds,
            recovery_cooldown_seconds=recovery_cooldown_seconds,
            max_recovery_attempts=max_recovery_attempts,
        )
        self.strategy = strategy
        self.broker = broker
        self.feed = feed
        self.on_error = on_error or self._default_error_handler
        self.feed_silence_seconds = feed_silence_seconds
        self.watchdog_poll_seconds = watchdog_poll_seconds
        self.halt_on_unhealthy = halt_on_unhealthy
        self.auto_recover = auto_recover
        self.recovery_cooldown_seconds = recovery_cooldown_seconds
        self.max_recovery_attempts = max_recovery_attempts
        self.max_event_age_seconds = (
            max_event_age_seconds
            if max_event_age_seconds is not None
            else getattr(feed, "max_event_age_seconds", None)
        )
        self._validate_event_age(self.max_event_age_seconds)
        self.on_health_change = on_health_change
        self.strategy_callback_timeout_seconds = strategy_callback_timeout_seconds
        self.lifecycle_version = negotiated_version
        self.execution_policy = execution_policy or default_live_execution_policy()
        self.strategy_config = strategy_config or BacktestConfig()
        self.strategy_runtime = LiveStrategyRuntime(
            broker,
            self.execution_policy,
            negotiated_version,
        )
        self.lifecycle_dispatcher = LiveLifecycleDispatcher(
            strategy,
            LIFECYCLE_V1,
            callback_timeout_seconds=strategy_callback_timeout_seconds,
            event_recorder=self._record_runtime_event,
        )

        self._running = False
        self._shutdown_event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wrapped_broker: ThreadSafeBrokerWrapper | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._signals_installed = False
        self._signal_shutdown_task: asyncio.Task[None] | None = None
        self._run_in_progress = False
        self._run_done_event = asyncio.Event()
        self._runtime_lock = asyncio.Lock()
        self._cleanup_lock = asyncio.Lock()
        self._runtime_state = RuntimeState.STOPPED
        self._runtime_transitions: list[RuntimeTransition] = []
        self._operational_events: deque[dict[str, Any]] = deque(
            maxlen=RETAINED_OPERATIONAL_EVENT_LIMIT
        )
        self._operational_event_count = 0
        self._operational_event_forwarded_count = 0
        self._broker_connect_attempted = False
        self._feed_start_attempted = False
        self._feed_close_required = False
        self._terminal_failure_reason: str | None = None
        self._stop_requested_reason: str | None = None
        self._run_event_count = 0
        self._last_cleanup_result: dict[str, str] | None = None
        self._release_failures: dict[str, str] = {}
        self._continuity = EventContinuityTracker()

        self._bar_count = 0
        self._event_count = 0
        self._event_kind_counts = dict.fromkeys(MarketEventKind, 0)
        self._error_count = 0
        self._last_bar_time: datetime | None = None
        self._last_event_time: datetime | None = None
        self._last_bar_received_at: datetime | None = None
        self._last_health = "stopped"
        self._recovery_requested_reason: str | None = None
        self._recovery_attempts = 0

    async def connect(self) -> None:
        """Acquire the broker and feed transactionally and become ready."""
        async with self._runtime_lock:
            if self._runtime_state is RuntimeState.READY:
                return
            if self._run_in_progress or self._runtime_state not in {
                RuntimeState.STOPPED,
                RuntimeState.FAILED,
            }:
                raise RuntimeError(
                    f"LiveEngine.connect() is invalid while {self._runtime_state.value}"
                )

            self._terminal_failure_reason = None
            self._stop_requested_reason = None
            self._last_cleanup_result = None
            self._release_failures.clear()
            logger.info("LiveEngine: Connecting...")
            try:
                self._transition(RuntimeState.PREFLIGHT, reason="connect_requested")
                await self._acquire_runtime(ready_state=RuntimeState.READY)
                self._loop = asyncio.get_running_loop()
                self._wrapped_broker = ThreadSafeBrokerWrapper(
                    self.broker,
                    self._loop,
                    self.strategy_runtime,
                )
                if not self._signals_installed:
                    self._signals_installed = self._install_signal_handlers()
            except BaseException as error:
                self._annotate_connect_failure(error)
                self._terminal_failure_reason = f"startup:{type(error).__name__}"
                cleanup = await self._finalize_runtime(
                    terminal_state=RuntimeState.FAILED,
                    reason=self._terminal_failure_reason,
                )
                self._add_cleanup_note(error, cleanup)
                raise

            logger.info("LiveEngine: Connected and ready")

    async def _acquire_runtime(
        self,
        *,
        ready_state: RuntimeState,
        attempt: int | None = None,
    ) -> None:
        """Acquire broker then feed, retaining partial-acquisition ownership."""
        self._transition(
            RuntimeState.CONNECTING_BROKER,
            reason="broker_connect",
            attempt=attempt,
        )
        self._broker_connect_attempted = True
        await self.broker.connect()
        self._transition(
            RuntimeState.RECONCILING,
            reason="broker_connected_and_reconciled",
            attempt=attempt,
        )
        self._transition(RuntimeState.STARTING_FEED, reason="feed_start", attempt=attempt)
        self._feed_start_attempted = True
        self._feed_close_required = True
        await self.feed.start()
        self._transition(ready_state, reason="runtime_acquired", attempt=attempt)

    async def run(self) -> None:
        """Main async loop - receives bars and dispatches to strategy."""
        if self._wrapped_broker is None or self._runtime_state is not RuntimeState.READY:
            raise RuntimeError("Call connect() before run()")
        if self._run_in_progress:
            raise RuntimeError("LiveEngine.run() is already active")

        self._run_in_progress = True
        self._run_done_event.clear()
        self._running = True
        self._recovery_requested_reason = None
        self._recovery_attempts = 0
        self._terminal_failure_reason = None
        self._stop_requested_reason = None
        self._run_event_count = 0
        self._shutdown_event.clear()
        logger.info("LiveEngine: Starting main loop")

        lifecycle_started = False
        callback_baseline = self.lifecycle_dispatcher.callback_counts
        failure: BaseException | None = None
        try:
            self._transition(RuntimeState.STARTING_STRATEGY, reason="strategy_start")
            lifecycle_started = True
            await self._dispatch_strategy(
                LifecyclePhase.RUN_START,
                self._wrapped_broker,
            )
            await self._dispatch_strategy(
                LifecyclePhase.CAUSAL_INITIALIZATION,
                self._wrapped_broker,
                self.strategy_config,
            )
            self._transition(RuntimeState.RUNNING, reason="strategy_started")
            self._watchdog_task = asyncio.create_task(
                self._watchdog_loop(),
                name="ml4t-live-watchdog",
            )
            while not self._shutdown_event.is_set():
                async for item in self._feed_items():
                    if self._shutdown_event.is_set():
                        logger.info("LiveEngine: Shutdown requested")
                        break
                    if self._current_broker_connected() is False:
                        self._terminal_failure_reason = "broker_disconnected"
                        self._shutdown_event.set()
                        raise RuntimeError(
                            "Broker disconnected before the next strategy event dispatch"
                        )

                    processing_time = datetime.now(UTC)
                    typed_event = item if isinstance(item, MarketEvent) else None
                    if typed_event is not None:
                        validate_event_timing(
                            typed_event,
                            processing_time=processing_time,
                            max_age_seconds=self.max_event_age_seconds,
                        )
                        disposition = self._continuity.validate(typed_event)
                        if disposition is ContinuityDisposition.DUPLICATE:
                            self._record_runtime_event(
                                "feed_duplicate_skipped",
                                source=typed_event.source,
                                asset=typed_event.asset,
                                kind=typed_event.kind.value,
                                provider_sequence=typed_event.provider_sequence,
                                event_time=typed_event.event_time.isoformat(),
                            )
                            continue
                        timestamp, data, context = strategy_input(
                            typed_event,
                            processing_time=processing_time,
                        )
                        event_kind = typed_event.kind
                    else:
                        timestamp, data, context = self._validate_legacy_feed_item(item)
                        event_kind = MarketEventKind.BAR

                    self._event_count += 1
                    self._run_event_count += 1
                    self._event_kind_counts[event_kind] += 1
                    self._last_event_time = timestamp
                    if event_kind is MarketEventKind.BAR:
                        self._bar_count += 1
                        self._last_bar_time = timestamp
                    self._last_bar_received_at = processing_time

                    complete_or_non_bar = (
                        typed_event is None
                        or event_kind is not MarketEventKind.BAR
                        or typed_event.completion is EventCompletion.COMPLETE
                    )
                    record_market_data = getattr(self.broker, "_record_market_data", None)
                    if callable(record_market_data) and complete_or_non_bar:
                        record_market_data(timestamp, data, context)

                    try:
                        if event_kind is MarketEventKind.BAR and complete_or_non_bar:
                            try:
                                await self.strategy_runtime.process_market_event(
                                    timestamp,
                                    data,
                                    context,
                                )
                            except BaseException as error:
                                _attach_runtime_error_context(
                                    error,
                                    component="strategy_runtime",
                                    operation="process_market_event",
                                    runtime_state=self._runtime_state,
                                    recovery_action=(
                                        "reconcile portable strategy state before restarting"
                                    ),
                                )
                                raise
                        await self._dispatch_strategy(
                            LifecyclePhase.MARKET_EVENT,
                            timestamp,
                            data,
                            context,
                            self._wrapped_broker,
                            event_time=timestamp,
                        )
                    except Exception as error:
                        self._error_count += 1
                        try:
                            self.on_error(error, timestamp, data)
                        except BaseException as handler_error:
                            error.add_note(
                                "on_error also failed: "
                                f"{type(handler_error).__name__}: "
                                f"{redact_sensitive(str(handler_error))}"
                            )
                        self._shutdown_event.set()
                        raise

                if self._shutdown_event.is_set():
                    break

                if self._recovery_requested_reason is None:
                    self._recovery_requested_reason = "feed_terminated"
                    self._transition(RuntimeState.DEGRADED, reason="feed_terminated")
                    logger.warning("LiveEngine: Feed terminated")

                if not self.auto_recover:
                    logger.warning(
                        "LiveEngine: Runtime degraded (%s) and auto recovery disabled",
                        self._recovery_requested_reason,
                    )
                    break

                recovered = await self._attempt_recovery(self._recovery_requested_reason)
                if not recovered:
                    break
        except BaseException as error:
            failure = error
            if isinstance(error, FeedContractError | FeedOverflowError):
                _attach_runtime_error_context(
                    error,
                    component="feed",
                    operation="validate",
                    runtime_state=self._runtime_state,
                    recovery_action=("restore the feed and establish continuity before restarting"),
                )
            elif not isinstance(error, asyncio.CancelledError):
                _attach_runtime_error_context(
                    error,
                    component="engine",
                    operation="run",
                    runtime_state=self._runtime_state,
                    recovery_action="inspect runtime diagnostics before restarting",
                )
            if (
                not isinstance(error, asyncio.CancelledError)
                and self._terminal_failure_reason is None
            ):
                self._terminal_failure_reason = f"runtime:{type(error).__name__}"
            if isinstance(error, FeedContinuityError | FeedOverflowError):
                if self._runtime_state is RuntimeState.RUNNING:
                    try:
                        self._transition(
                            RuntimeState.DEGRADED,
                            reason=f"feed_safety:{type(error).__name__}",
                        )
                    except BaseException as transition_error:
                        error.add_note(
                            "feed safety transition also failed: "
                            f"{type(transition_error).__name__}: "
                            f"{redact_sensitive(str(transition_error))}"
                        )
                try:
                    self._record_runtime_event(
                        "feed_safety_halt",
                        detail=error.to_dict(),
                    )
                except BaseException as audit_error:
                    error.add_note(
                        "feed safety audit also failed: "
                        f"{type(audit_error).__name__}: "
                        f"{redact_sensitive(str(audit_error))}"
                    )
        finally:
            self._running = False
            try:
                await self._cancel_watchdog()
            except BaseException as finalization_error:
                failure = self._retain_failure(
                    failure,
                    finalization_error,
                    context="watchdog cancellation",
                )
                self._terminal_failure_reason = (
                    f"watchdog_cancel:{type(finalization_error).__name__}"
                )
            try:
                self._emit_health_transition(self.runtime_status())
            except BaseException as finalization_error:
                failure = self._retain_failure(
                    failure,
                    finalization_error,
                    context="health finalization",
                )
                self._terminal_failure_reason = (
                    f"health_finalization:{type(finalization_error).__name__}"
                )
            if lifecycle_started:
                try:
                    await self._dispatch_strategy(
                        LifecyclePhase.RUN_END,
                        self._wrapped_broker,
                    )
                except BaseException as finalization_error:
                    self._terminal_failure_reason = (
                        f"strategy_end:{type(finalization_error).__name__}"
                    )
                    failure = self._retain_failure(
                        failure,
                        finalization_error,
                        context="on_end",
                    )
            if failure is None:
                try:
                    self.lifecycle_dispatcher.validate_completed_run(
                        self._run_event_count,
                        baseline=callback_baseline,
                    )
                except BaseException as finalization_error:
                    failure = finalization_error
                    self._terminal_failure_reason = (
                        f"lifecycle_validation:{type(finalization_error).__name__}"
                    )
            try:
                self.lifecycle_dispatcher.close()
            except BaseException as finalization_error:
                failure = self._retain_failure(
                    failure,
                    finalization_error,
                    context="strategy worker shutdown",
                )
                self._terminal_failure_reason = (
                    f"strategy_worker:{type(finalization_error).__name__}"
                )
            terminal_state = (
                RuntimeState.FAILED
                if self._terminal_failure_reason is not None
                else RuntimeState.STOPPED
            )
            cleanup = await self._finalize_runtime(
                terminal_state=terminal_state,
                reason=(
                    self._terminal_failure_reason or self._stop_requested_reason or "run_completed"
                ),
            )
            if failure is not None:
                self._add_cleanup_note(failure, cleanup)
            elif any(value.startswith("failed:") for value in cleanup.values()):
                failure = RuntimeCleanupError(cleanup)
            elif self._terminal_failure_reason is not None:
                failure = RuntimeFailureError(self._terminal_failure_reason)
            self._run_in_progress = False
            self._run_done_event.set()
            logger.info(
                "LiveEngine: Stopped. Bars: %s, Errors: %s",
                self._bar_count,
                self._error_count,
            )

        if failure is not None:
            raise failure.with_traceback(failure.__traceback__)

    async def _dispatch_strategy(
        self,
        phase: LifecyclePhase,
        *args: Any,
        event_time: datetime | None = None,
    ) -> Any:
        """Expose the active causal phase while one strategy callback executes."""
        self.strategy_runtime.active_phase = phase
        self.strategy_runtime.current_event_time = event_time
        try:
            try:
                return await self.lifecycle_dispatcher.dispatch(
                    phase,
                    *args,
                    event_time=event_time,
                )
            except BaseException as error:
                callback = self.lifecycle_dispatcher.contract.phase_spec(phase).callback
                _attach_runtime_error_context(
                    error,
                    component="strategy",
                    operation=callback,
                    runtime_state=self._runtime_state,
                    recovery_action="correct the strategy callback before restarting",
                )
                raise
        finally:
            self.strategy_runtime.active_phase = None
            self.strategy_runtime.current_event_time = None

    async def _feed_items(self) -> AsyncIterator[Any]:
        """Read feed items while retaining public failure context."""
        iterator = aiter(self.feed)
        while True:
            try:
                item = await anext(iterator)
            except StopAsyncIteration:
                return
            except BaseException as error:
                _attach_runtime_error_context(
                    error,
                    component="feed",
                    operation="read",
                    runtime_state=self._runtime_state,
                    recovery_action=("restore the feed and establish continuity before restarting"),
                )
                raise
            yield item

    def _annotate_connect_failure(self, error: BaseException) -> None:
        contexts = {
            RuntimeState.PREFLIGHT: (
                "engine",
                "preflight",
                "correct the preflight failure, then call connect() again",
            ),
            RuntimeState.CONNECTING_BROKER: (
                "broker",
                "connect",
                "correct the broker failure, then call connect() again",
            ),
            RuntimeState.RECONCILING: (
                "broker",
                "reconcile",
                "reconcile broker and persisted state before calling connect() again",
            ),
            RuntimeState.STARTING_FEED: (
                "feed",
                "start",
                "correct the feed failure, then call connect() again",
            ),
        }
        component, operation, recovery_action = contexts.get(
            self._runtime_state,
            (
                "engine",
                "connect",
                "inspect startup diagnostics before calling connect() again",
            ),
        )
        _attach_runtime_error_context(
            error,
            component=component,
            operation=operation,
            runtime_state=self._runtime_state,
            recovery_action=recovery_action,
        )

    async def _watchdog_loop(self) -> None:
        """Monitor runtime health and request recovery/escalation when needed."""
        try:
            while self._running and not self._shutdown_event.is_set():
                status = self.runtime_status()
                self._emit_health_transition(status)
                health = status["health"]

                if health in RECOVERABLE_HEALTH_STATES and self._recovery_requested_reason is None:
                    if self.auto_recover:
                        self._recovery_requested_reason = health
                        self._transition(RuntimeState.DEGRADED, reason=health)
                        logger.warning(
                            "LiveEngine: Scheduling recovery due to %s",
                            health,
                        )
                        self._stop_feed_once()
                    elif self.halt_on_unhealthy:
                        self._transition(RuntimeState.DEGRADED, reason=health)
                        self._stop_requested_reason = health
                        logger.error(
                            "LiveEngine: Halting due to unhealthy runtime state %s",
                            health,
                        )
                        self._shutdown_event.set()
                        self._stop_feed_once()

                await asyncio.sleep(self.watchdog_poll_seconds)
        except asyncio.CancelledError:
            return
        except BaseException as error:
            self._terminal_failure_reason = f"watchdog:{type(error).__name__}"
            self._shutdown_event.set()
            self._stop_feed_once()
            logger.error(
                "LiveEngine: Watchdog failed: %s",
                redact_sensitive(str(error)),
            )

    async def _attempt_recovery(self, reason: str) -> bool:
        """Attempt broker/feed recovery after a watchdog-triggered failure."""
        while self._recovery_attempts < self.max_recovery_attempts:
            self._recovery_attempts += 1
            attempt = self._recovery_attempts
            started_at = monotonic()
            self._transition(RuntimeState.RECOVERING, reason=reason, attempt=attempt)
            logger.warning(
                "LiveEngine: Recovery attempt %s/%s after %s",
                attempt,
                self.max_recovery_attempts,
                reason,
            )
            self._record_runtime_event(
                "engine_recovery_attempt",
                attempt=attempt,
                max_attempts=self.max_recovery_attempts,
                reason=reason,
            )

            cleanup = await self._release_runtime_resources()

            if any(value.startswith("failed:") for value in cleanup.values()):
                duration = monotonic() - started_at
                self._transition(
                    RuntimeState.DEGRADED,
                    reason="recovery_cleanup_failed",
                    attempt=attempt,
                    forward=False,
                )
                self._record_runtime_event(
                    "engine_recovery_failed",
                    forward=False,
                    attempt=attempt,
                    reason=reason,
                    error="runtime cleanup failed",
                    duration_seconds=duration,
                    last_known_sequence=self._bar_count,
                    cleanup_result=cleanup,
                    terminal_status=self._runtime_state.value,
                )
                await asyncio.sleep(self.recovery_cooldown_seconds)
                continue

            await asyncio.sleep(self.recovery_cooldown_seconds)

            try:
                await self._acquire_runtime(
                    ready_state=RuntimeState.RUNNING,
                    attempt=attempt,
                )
            except Exception as exc:
                failed_cleanup = await self._release_runtime_resources()
                duration = monotonic() - started_at
                self._transition(
                    RuntimeState.DEGRADED,
                    reason=f"recovery_attempt_failed:{type(exc).__name__}",
                    attempt=attempt,
                    forward=False,
                )
                logger.error(
                    "LiveEngine: Recovery attempt %s failed: %s",
                    attempt,
                    redact_sensitive(str(exc)),
                )
                self._record_runtime_event(
                    "engine_recovery_failed",
                    forward=False,
                    attempt=attempt,
                    reason=reason,
                    error=redact_sensitive(str(exc)),
                    duration_seconds=duration,
                    last_known_sequence=self._bar_count,
                    cleanup_result={"before": cleanup, "after": failed_cleanup},
                    terminal_status=self._runtime_state.value,
                )
                continue

            self._continuity.mark_recovery()
            self._recovery_requested_reason = None
            self._last_bar_received_at = None
            duration = monotonic() - started_at
            logger.info("LiveEngine: Recovery succeeded on attempt %s", attempt)
            self._record_runtime_event(
                "engine_recovery_succeeded",
                attempt=attempt,
                reason=reason,
                duration_seconds=duration,
                last_known_sequence=self._bar_count,
                cleanup_result=cleanup,
                terminal_status=self._runtime_state.value,
            )
            return True

        logger.error(
            "LiveEngine: Recovery failed after %s attempts; stopping",
            self.max_recovery_attempts,
        )
        self._record_runtime_event(
            "engine_recovery_exhausted",
            forward=False,
            max_attempts=self.max_recovery_attempts,
            reason=reason,
            last_known_sequence=self._bar_count,
            terminal_status=RuntimeState.FAILED.value,
        )
        self._terminal_failure_reason = f"recovery_exhausted:{reason}"
        self._shutdown_event.set()
        return False

    async def _cancel_watchdog(self) -> None:
        if self._watchdog_task is None:
            return
        self._watchdog_task.cancel()
        await asyncio.gather(self._watchdog_task, return_exceptions=True)
        self._watchdog_task = None

    async def stop(self) -> None:
        """Request shutdown and release resources exactly once."""
        logger.info("LiveEngine: Stopping...")
        self._stop_requested_reason = self._stop_requested_reason or "stop_requested"
        self._shutdown_event.set()
        await self._cancel_watchdog()
        if self._run_in_progress:
            self._stop_feed_once()
            await self._run_done_event.wait()
            cleanup = self._last_cleanup_result or {}
            if any(value.startswith("failed:") for value in cleanup.values()):
                raise RuntimeCleanupError(cleanup)
        else:
            async with self._runtime_lock:
                cleanup = await self._finalize_runtime(
                    terminal_state=RuntimeState.STOPPED,
                    reason=self._stop_requested_reason,
                )
            if any(value.startswith("failed:") for value in cleanup.values()):
                raise RuntimeCleanupError(cleanup)
        logger.info("LiveEngine: Stopped")

    def _stop_feed_once(self) -> str:
        if not self._feed_start_attempted:
            return "not_acquired"
        self._feed_start_attempted = False
        try:
            self.feed.stop()
        except BaseException as error:
            self._feed_start_attempted = True
            logger.warning(
                "LiveEngine: Feed stop failed: %s",
                redact_sensitive(str(error)),
            )
            result = f"failed:{type(error).__name__}"
            self._release_failures["feed"] = result
            return result
        self._release_failures.pop("feed", None)
        return "released"

    async def _disconnect_broker_once(self) -> str:
        if not self._broker_connect_attempted:
            return "not_acquired"
        self._broker_connect_attempted = False
        try:
            await self.broker.disconnect()
        except BaseException as error:
            self._broker_connect_attempted = True
            logger.warning(
                "LiveEngine: Broker disconnect failed: %s",
                redact_sensitive(str(error)),
            )
            result = f"failed:{type(error).__name__}"
            self._release_failures["broker"] = result
            return result
        self._release_failures.pop("broker", None)
        return "released"

    async def _close_feed_once(self) -> str:
        if not self._feed_close_required:
            return "not_acquired"
        close = getattr(self.feed, "close", None)
        if not callable(close):
            self._feed_close_required = False
            return "not_supported"
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except BaseException as error:
            logger.warning(
                "LiveEngine: Feed close failed: %s",
                redact_sensitive(str(error)),
            )
            failure = f"failed:{type(error).__name__}"
            self._release_failures["feed_close"] = failure
            return failure
        self._feed_close_required = False
        self._release_failures.pop("feed_close", None)
        return "released"

    async def _release_runtime_resources(self) -> dict[str, str]:
        """Release feed then broker in reverse acquisition order."""
        feed = self._stop_feed_once()
        feed_close = await self._close_feed_once()
        broker = await self._disconnect_broker_once()
        result = {
            "feed": self._release_failures.get("feed", feed),
            "broker": self._release_failures.get("broker", broker),
        }
        if feed_close != "not_supported":
            result["feed_close"] = self._release_failures.get("feed_close", feed_close)
        return result

    async def _finalize_runtime(
        self,
        *,
        terminal_state: RuntimeState,
        reason: str,
    ) -> dict[str, str]:
        """Serialize cleanup and retain its exact result without masking a primary error."""
        async with self._cleanup_lock:
            if (
                self._runtime_state is terminal_state
                and not self._feed_start_attempted
                and not self._feed_close_required
                and not self._broker_connect_attempted
                and not self._signals_installed
            ):
                return self._last_cleanup_result or {
                    "watchdog": "not_running",
                    "feed": "not_acquired",
                    "broker": "not_acquired",
                    "signals": "not_installed",
                }

            transition_error = "none"
            if self._runtime_state is not RuntimeState.STOPPING:
                try:
                    self._transition(
                        RuntimeState.STOPPING,
                        reason=reason,
                        forward=False,
                    )
                except BaseException as error:
                    transition_error = f"failed:{type(error).__name__}"

            watchdog = "not_running"
            if self._watchdog_task is not None:
                try:
                    await self._cancel_watchdog()
                    watchdog = "released"
                except BaseException as error:
                    watchdog = f"failed:{type(error).__name__}"

            resources = await self._release_runtime_resources()
            try:
                signals = self._remove_signal_handlers()
            except BaseException as error:
                signals = f"failed:{type(error).__name__}"
            result = {
                "state_transition": transition_error,
                "watchdog": watchdog,
                **resources,
                "signals": signals,
            }
            if any(value.startswith("failed:") for value in result.values()):
                terminal_state = RuntimeState.FAILED
                self._terminal_failure_reason = self._terminal_failure_reason or "cleanup_failed"
            try:
                self._transition(terminal_state, reason=reason, forward=False)
            except BaseException as error:
                result["terminal_transition"] = f"failed:{type(error).__name__}"
                self._runtime_state = terminal_state
            self._record_runtime_event(
                "engine_cleanup_completed",
                forward=False,
                reason=reason,
                cleanup_result=result,
                last_known_sequence=self._bar_count,
                terminal_status=terminal_state.value,
            )
            self._last_cleanup_result = result
            self._release_failures.clear()
            return result

    @staticmethod
    def _add_cleanup_note(error: BaseException, cleanup: dict[str, str]) -> None:
        failures = {key: value for key, value in cleanup.items() if value.startswith("failed:")}
        if failures:
            error.add_note(f"runtime cleanup failures: {failures}")

    @staticmethod
    def _retain_failure(
        current: BaseException | None,
        additional: BaseException,
        *,
        context: str,
    ) -> BaseException:
        if current is None:
            return additional
        current.add_note(
            f"{context} also failed: {type(additional).__name__}: "
            f"{redact_sensitive(str(additional))}"
        )
        return current

    def _transition(
        self,
        current: RuntimeState,
        *,
        reason: str,
        attempt: int | None = None,
        forward: bool = True,
    ) -> None:
        previous = self._runtime_state
        if current is previous:
            return
        if current not in _ALLOWED_RUNTIME_TRANSITIONS[previous]:
            raise RuntimeError(
                f"invalid LiveEngine runtime transition {previous.value} -> {current.value}"
            )
        transition = RuntimeTransition(
            occurred_at=datetime.now(UTC),
            previous=previous,
            current=current,
            reason=reason,
            attempt=attempt,
            last_known_sequence=self._bar_count,
        )
        self._runtime_state = current
        self._runtime_transitions.append(transition)
        self._record_runtime_event(
            "engine_runtime_transition",
            forward=forward,
            occurred_at=transition.occurred_at.isoformat(),
            previous=previous.value,
            current=current.value,
            reason=reason,
            attempt=attempt,
            last_known_sequence=self._bar_count,
        )

    @staticmethod
    def _validate_runtime_configuration(
        *,
        feed_silence_seconds: float | None,
        watchdog_poll_seconds: float,
        recovery_cooldown_seconds: float,
        max_recovery_attempts: int,
    ) -> None:
        for name, value, allow_zero in (
            ("feed_silence_seconds", feed_silence_seconds, False),
            ("watchdog_poll_seconds", watchdog_poll_seconds, False),
            ("recovery_cooldown_seconds", recovery_cooldown_seconds, True),
        ):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be a finite number")
            if not math.isfinite(value) or value < 0 or (not allow_zero and value == 0):
                qualifier = "non-negative" if allow_zero else "positive"
                raise ValueError(f"{name} must be finite and {qualifier}")
        if (
            isinstance(max_recovery_attempts, bool)
            or not isinstance(max_recovery_attempts, int)
            or max_recovery_attempts < 0
        ):
            raise ValueError("max_recovery_attempts must be a non-negative integer")

    @staticmethod
    def _validate_event_age(value: float | None) -> None:
        if value is None:
            return
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError("max_event_age_seconds must be finite and positive or None")

    @staticmethod
    def _validate_legacy_feed_item(
        item: object,
    ) -> tuple[datetime, dict[str, dict[str, Any]], dict[str, Any]]:
        """Validate the temporary tuple contract retained for experimental feeds."""
        if not isinstance(item, tuple) or len(item) != 3:
            raise TypeError("feed must emit MarketEvent or a three-item experimental tuple")
        timestamp, data, context = item
        timestamp = utc_datetime(timestamp, "legacy feed timestamp")
        if not isinstance(data, dict) or not all(
            isinstance(asset, str) and isinstance(payload, dict) for asset, payload in data.items()
        ):
            raise TypeError("legacy feed data must map asset strings to payload mappings")
        if not isinstance(context, dict):
            raise TypeError("legacy feed context must be a mapping")
        return timestamp, cast("dict[str, dict[str, Any]]", data), cast("dict[str, Any]", context)

    @staticmethod
    def _validate_strategy_lifecycle(strategy: Strategy) -> None:
        """Reject lifecycle surfaces that require unavailable historical state."""
        strategy_type = type(strategy)
        callback_names = tuple(dir(strategy_type))
        require_historical_strategy_compatibility(strategy_type.__name__, callback_names)
        if getattr(strategy_type, "on_before_risk", None) is not None:
            raise HistoricalStrategyCompatibilityError(
                strategy_type.__name__,
                "on_before_risk",
                LifecyclePhase.PRE_OPEN,
            )
        parameters = tuple(inspect.signature(strategy_type.on_prepare).parameters.values())
        positional = tuple(
            parameter
            for parameter in parameters
            if parameter.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        )
        if any(parameter.name == "timestamps" for parameter in parameters) or len(positional) > 3:
            raise HistoricalStrategyCompatibilityError(
                strategy_type.__name__,
                "on_prepare(timestamps)",
                LifecyclePhase.CAUSAL_INITIALIZATION,
            )

    def _install_signal_handlers(self) -> bool:
        """Install SIGINT/SIGTERM handlers for graceful shutdown."""
        loop = asyncio.get_running_loop()

        def handler(sig: signal.Signals) -> None:
            logger.info("LiveEngine: Received %s", sig.name)
            self._request_signal_shutdown(sig)

        installed: list[signal.Signals] = []
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, handler, sig)
            except (NotImplementedError, RuntimeError):
                for installed_signal in installed:
                    loop.remove_signal_handler(installed_signal)
                logger.info(
                    "LiveEngine: Process signal handlers are unavailable; use stop() to shut down"
                )
                return False
            installed.append(sig)
        return True

    def _request_signal_shutdown(self, sig: signal.Signals) -> None:
        """Schedule the same transactional stop path used by API callers."""
        if self._signal_shutdown_task is not None and not self._signal_shutdown_task.done():
            return
        self._stop_requested_reason = f"signal:{sig.name}"
        self._shutdown_event.set()
        task = asyncio.create_task(self.stop(), name="ml4t-live-signal-shutdown")
        self._signal_shutdown_task = task
        task.add_done_callback(self._signal_shutdown_completed)

    def _signal_shutdown_completed(self, task: asyncio.Task[None]) -> None:
        self._signal_shutdown_task = None
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "LiveEngine: Signal shutdown failed: %s",
                redact_sensitive(str(error)),
            )

    def _remove_signal_handlers(self) -> str:
        if not self._signals_installed:
            return "not_installed"
        loop = self._loop
        self._signals_installed = False
        if loop is None or loop.is_closed():
            return "loop_unavailable"
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
        return "released"

    def _default_error_handler(self, error: Exception, timestamp: datetime, data: dict) -> None:
        """Default error handler - log and continue."""
        logger.error(
            "Strategy error at %s: %s: %s",
            timestamp,
            type(error).__name__,
            redact_sensitive(str(error)),
        )
        self._record_runtime_event(
            "strategy_error",
            timestamp=timestamp.isoformat(),
            error_type=type(error).__name__,
            error=redact_sensitive(str(error)),
        )

    def _emit_health_transition(self, status: dict[str, Any]) -> None:
        """Log and callback on health-state changes."""
        health = str(status["health"])
        if health == self._last_health:
            return

        logger.info("LiveEngine: Health transition %s -> %s", self._last_health, health)
        self._record_runtime_event(
            "engine_health_transition",
            previous=self._last_health,
            current=health,
            detail=status,
        )
        self._last_health = health
        if self.on_health_change is not None:
            self.on_health_change(health, status)

    def _record_runtime_event(
        self,
        event: str,
        *,
        forward: bool = True,
        **payload: Any,
    ) -> None:
        """Retain an operational event and forward it while the broker is acquired."""
        redacted_payload = redact_sensitive(payload)
        self._operational_event_count += 1
        self._operational_events.append({"event": event, **redacted_payload})
        if not forward:
            return
        recorder = getattr(self.broker, "record_event", None)
        if callable(recorder):
            recorder(event, **redacted_payload)
            self._operational_event_forwarded_count += 1

    def _normalize_utc(self, timestamp: datetime) -> datetime:
        if timestamp.tzinfo is None:
            return timestamp.replace(tzinfo=UTC)
        return timestamp.astimezone(UTC)

    def _current_broker_connected(self) -> bool | None:
        broker_connected = getattr(self.broker, "is_connected", None)
        if isinstance(broker_connected, bool):
            return broker_connected
        return None

    def _equity_symbols(self) -> list[str]:
        stock_symbols = getattr(self.feed, "_stock_symbols", None)
        if isinstance(stock_symbols, list) and stock_symbols:
            return [str(symbol).upper() for symbol in stock_symbols]

        if self.feed.__class__.__name__ == "IBDataFeed":
            symbols = getattr(self.feed, "symbols", None)
            if isinstance(symbols, list):
                return [str(symbol).upper() for symbol in symbols]

        return []

    def _next_trading_day(self, current_day: datetime) -> datetime:
        candidate = current_day + timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        return candidate

    def _equity_session_status(self, now: datetime) -> dict[str, Any]:
        symbols = self._equity_symbols()
        if not symbols:
            return {
                "market": "continuous",
                "next_boundary": None,
                "tracked_symbols": [],
            }

        now_et = now.astimezone(US_EASTERN)
        open_dt = datetime.combine(now_et.date(), US_EQUITY_OPEN, tzinfo=US_EASTERN)
        close_dt = datetime.combine(now_et.date(), US_EQUITY_CLOSE, tzinfo=US_EASTERN)

        if now_et.weekday() >= 5:
            next_open = datetime.combine(
                self._next_trading_day(now_et).date(),
                US_EQUITY_OPEN,
                tzinfo=US_EASTERN,
            )
            market = "closed"
            next_boundary = next_open
        elif now_et < open_dt:
            market = "pre_open"
            next_boundary = open_dt
        elif now_et < close_dt:
            market = "open"
            next_boundary = close_dt
        else:
            market = "closed"
            next_open = datetime.combine(
                self._next_trading_day(now_et).date(),
                US_EQUITY_OPEN,
                tzinfo=US_EASTERN,
            )
            next_boundary = next_open

        return {
            "market": market,
            "next_boundary": next_boundary.astimezone(UTC),
            "tracked_symbols": symbols,
        }

    def runtime_status(self, now: datetime | None = None) -> dict[str, Any]:
        """Return engine runtime health and session context."""
        reference_now = self._normalize_utc(now or datetime.now(UTC))
        session = self._equity_session_status(reference_now)
        broker_connected = self._current_broker_connected()
        feed_stats = getattr(self.feed, "stats", None)
        if not isinstance(feed_stats, dict):
            feed_stats = None

        last_bar_age_seconds: float | None = None
        if self._last_bar_received_at is not None:
            last_bar_age_seconds = max(
                0.0,
                (reference_now - self._last_bar_received_at).total_seconds(),
            )

        if self._runtime_state is RuntimeState.FAILED:
            health = "failed"
        elif self._runtime_state is RuntimeState.READY:
            health = "ready"
        elif not self._running:
            health = "stopped"
        elif broker_connected is False:
            health = "broker_disconnected"
        elif session["market"] not in {"open", "continuous"}:
            health = "idle_market_closed"
        elif last_bar_age_seconds is None:
            health = "waiting_for_data"
        elif (
            self.feed_silence_seconds is not None
            and last_bar_age_seconds > self.feed_silence_seconds
        ):
            health = "feed_silent"
        else:
            health = "ok"

        return {
            "running": self._running,
            "runtime_state": self._runtime_state.value,
            "terminal_failure_reason": self._terminal_failure_reason,
            "event_count": self._event_count,
            "event_kind_counts": {
                kind.value: count for kind, count in self._event_kind_counts.items()
            },
            "bar_count": self._bar_count,
            "error_count": self._error_count,
            "last_event_time": self._last_event_time,
            "last_bar_time": self._last_bar_time,
            "last_bar_received_at": self._last_bar_received_at,
            "last_bar_age_seconds": last_bar_age_seconds,
            "broker_connected": broker_connected,
            "session_state": session["market"],
            "next_session_boundary": session["next_boundary"],
            "tracked_symbols": session["tracked_symbols"],
            "health": health,
            "halt_on_unhealthy": self.halt_on_unhealthy,
            "auto_recover": self.auto_recover,
            "max_event_age_seconds": self.max_event_age_seconds,
            "feed": feed_stats,
            "continuity": self._continuity.snapshot(),
            "recovery_requested": self._recovery_requested_reason,
            "recovery_attempts": self._recovery_attempts,
            "lifecycle_version": self.lifecycle_version.value,
            "execution_policy": self.execution_policy.to_dict(),
            "target_intent_count": len(self.strategy_runtime.targets),
            "position_rule_state_count": len(self.strategy_runtime.position_rule_states),
            "callback_counts": {
                phase.value: count
                for phase, count in self.lifecycle_dispatcher.callback_counts.items()
            },
            "diagnostics": {
                "callback_invocations_total": self.lifecycle_dispatcher.invocation_count,
                "callback_invocations_retained": len(self.lifecycle_dispatcher.invocations),
                "callback_invocations_dropped": (
                    self.lifecycle_dispatcher.dropped_invocation_count
                ),
                "operational_events_total": self._operational_event_count,
                "operational_events_forwarded": self._operational_event_forwarded_count,
                "operational_events_retained": len(self._operational_events),
                "operational_events_dropped": max(
                    0,
                    self._operational_event_count - len(self._operational_events),
                ),
            },
            "last_cleanup_result": self._last_cleanup_result,
        }

    @property
    def runtime_state(self) -> RuntimeState:
        """Return the current transactional runtime state."""
        return self._runtime_state

    @property
    def runtime_transitions(self) -> tuple[RuntimeTransition, ...]:
        """Return retained state transitions in occurrence order."""
        return tuple(self._runtime_transitions)

    @property
    def operational_events(self) -> tuple[dict[str, Any], ...]:
        """Return redacted structured runtime diagnostics."""
        return tuple(dict(event) for event in self._operational_events)

    @property
    def stats(self) -> dict[str, Any]:
        """Get engine statistics and runtime health."""
        return self.runtime_status()
