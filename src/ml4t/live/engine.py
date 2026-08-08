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
import signal
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from typing import Any
from zoneinfo import ZoneInfo

from ml4t.backtest import Strategy
from ml4t.specs import (
    LIFECYCLE_V1,
    ExecutionPolicy,
    HistoricalStrategyCompatibilityError,
    LifecyclePhase,
    LifecycleVersion,
    negotiate_lifecycle_version,
    require_historical_strategy_compatibility,
)

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
        halt_on_error: bool = False,
        feed_silence_seconds: float | None = None,
        watchdog_poll_seconds: float = 1.0,
        halt_on_unhealthy: bool = False,
        auto_recover: bool = False,
        recovery_cooldown_seconds: float = 5.0,
        max_recovery_attempts: int = 3,
        on_health_change: Callable[[str, dict[str, Any]], None] | None = None,
        lifecycle_version: LifecycleVersion | str = LifecycleVersion.V1,
        execution_policy: ExecutionPolicy | None = None,
    ):
        """Initialize LiveEngine.

        Args:
            strategy: Strategy instance to execute.
            broker: Async broker implementation.
            feed: Data feed providing timestamp, data, context tuples.
            on_error: Custom error handler callback.
            halt_on_error: Deprecated compatibility input. Lifecycle v1 always aborts and reraises
                strategy exceptions after cleanup.
            feed_silence_seconds: Optional threshold for degraded feed reporting.
            watchdog_poll_seconds: Poll interval for runtime health monitoring.
            halt_on_unhealthy: Stop the engine when watchdog detects a degraded state.
            auto_recover: Attempt reconnect/restart when watchdog detects a recoverable state.
            recovery_cooldown_seconds: Delay between recovery attempts.
            max_recovery_attempts: Maximum recovery attempts before stopping.
            on_health_change: Optional callback invoked when runtime health changes.
            lifecycle_version: Portable strategy lifecycle version.
            execution_policy: Explicit live execution capabilities and behavior.
        """
        negotiated_version = negotiate_lifecycle_version(lifecycle_version)
        self._validate_strategy_lifecycle(strategy)
        self.strategy = strategy
        self.broker = broker
        self.feed = feed
        self.on_error = on_error or self._default_error_handler
        self.halt_on_error = halt_on_error
        self.feed_silence_seconds = feed_silence_seconds
        self.watchdog_poll_seconds = watchdog_poll_seconds
        self.halt_on_unhealthy = halt_on_unhealthy
        self.auto_recover = auto_recover
        self.recovery_cooldown_seconds = recovery_cooldown_seconds
        self.max_recovery_attempts = max_recovery_attempts
        self.on_health_change = on_health_change
        self.lifecycle_version = negotiated_version
        self.execution_policy = execution_policy or default_live_execution_policy()
        self.strategy_runtime = LiveStrategyRuntime(
            broker,
            self.execution_policy,
            negotiated_version,
        )
        self.lifecycle_dispatcher = LiveLifecycleDispatcher(
            strategy,
            LIFECYCLE_V1,
            event_recorder=self._record_runtime_event,
        )

        self._running = False
        self._shutdown_event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wrapped_broker: ThreadSafeBrokerWrapper | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._signals_installed = False
        self._run_in_progress = False
        self._run_done_event = asyncio.Event()

        self._bar_count = 0
        self._error_count = 0
        self._last_bar_time: datetime | None = None
        self._last_bar_received_at: datetime | None = None
        self._last_health = "stopped"
        self._recovery_requested_reason: str | None = None
        self._recovery_attempts = 0

    async def connect(self) -> None:
        """Connect to broker and data feed.

        Must be called before run().
        """
        logger.info("LiveEngine: Connecting...")
        await self._connect_runtime()

        self._loop = asyncio.get_running_loop()
        if self._wrapped_broker is None:
            self._wrapped_broker = ThreadSafeBrokerWrapper(
                self.broker,
                self._loop,
                self.strategy_runtime,
            )

        if not self._signals_installed:
            self._install_signal_handlers()
            self._signals_installed = True

        logger.info("LiveEngine: Connected and ready")

    async def _connect_runtime(self) -> None:
        """Connect broker and start feed without rebuilding wrappers."""
        await self.broker.connect()
        await self.feed.start()

    async def run(self) -> None:
        """Main async loop - receives bars and dispatches to strategy."""
        if self._wrapped_broker is None:
            raise RuntimeError("Call connect() before run()")
        if self._run_in_progress:
            raise RuntimeError("LiveEngine.run() is already active")

        self._run_in_progress = True
        self._run_done_event.clear()
        self._running = True
        self._recovery_requested_reason = None
        self._recovery_attempts = 0
        self._shutdown_event.clear()
        logger.info("LiveEngine: Starting main loop")

        lifecycle_initialized = False
        failure: BaseException | None = None
        try:
            await self._dispatch_strategy(
                LifecyclePhase.RUN_START,
                self._wrapped_broker,
            )
            await self._dispatch_strategy(
                LifecyclePhase.CAUSAL_INITIALIZATION,
                self._wrapped_broker,
                None,
            )
            lifecycle_initialized = True
            self._watchdog_task = asyncio.create_task(
                self._watchdog_loop(),
                name="ml4t-live-watchdog",
            )
            while not self._shutdown_event.is_set():
                async for timestamp, data, context in self.feed:
                    if self._shutdown_event.is_set():
                        logger.info("LiveEngine: Shutdown requested")
                        break

                    self._bar_count += 1
                    self._last_bar_time = timestamp
                    self._last_bar_received_at = datetime.now(UTC)

                    record_market_data = getattr(self.broker, "_record_market_data", None)
                    if callable(record_market_data):
                        record_market_data(timestamp, data, context)

                    try:
                        await self.strategy_runtime.process_market_event(timestamp, data, context)
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
                                f"{type(handler_error).__name__}: {handler_error}"
                            )
                        self._shutdown_event.set()
                        raise

                if self._shutdown_event.is_set():
                    break

                if self._recovery_requested_reason is None:
                    logger.info("LiveEngine: Feed ended")
                    break

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
        finally:
            self._running = False
            try:
                await self._cancel_watchdog()
                self._emit_health_transition(self.runtime_status())
                if lifecycle_initialized:
                    try:
                        await self._dispatch_strategy(
                            LifecyclePhase.RUN_END,
                            self._wrapped_broker,
                        )
                    except BaseException as finalization_error:
                        if failure is None:
                            failure = finalization_error
                        else:
                            failure.add_note(
                                "on_end also failed during cleanup: "
                                f"{type(finalization_error).__name__}: {finalization_error}"
                            )
                if failure is None:
                    self.lifecycle_dispatcher.validate_completed_run(self._bar_count)
            finally:
                self.lifecycle_dispatcher.close()
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
            return await self.lifecycle_dispatcher.dispatch(
                phase,
                *args,
                event_time=event_time,
            )
        finally:
            self.strategy_runtime.active_phase = None
            self.strategy_runtime.current_event_time = None

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
                        logger.warning(
                            "LiveEngine: Scheduling recovery due to %s",
                            health,
                        )
                        self.feed.stop()
                    elif self.halt_on_unhealthy:
                        logger.error(
                            "LiveEngine: Halting due to unhealthy runtime state %s",
                            health,
                        )
                        self._shutdown_event.set()
                        self.feed.stop()

                await asyncio.sleep(self.watchdog_poll_seconds)
        except asyncio.CancelledError:
            return

    async def _attempt_recovery(self, reason: str) -> bool:
        """Attempt broker/feed recovery after a watchdog-triggered failure."""
        while self._recovery_attempts < self.max_recovery_attempts:
            self._recovery_attempts += 1
            attempt = self._recovery_attempts
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

            try:
                self.feed.stop()
            except Exception as exc:
                logger.warning(
                    "LiveEngine: Feed stop during recovery failed: %s",
                    redact_sensitive(str(exc)),
                )

            try:
                await self.broker.disconnect()
            except Exception as exc:
                logger.warning(
                    "LiveEngine: Broker disconnect during recovery failed: %s",
                    redact_sensitive(str(exc)),
                )

            await asyncio.sleep(self.recovery_cooldown_seconds)

            try:
                await self._connect_runtime()
            except Exception as exc:
                logger.error(
                    "LiveEngine: Recovery attempt %s failed: %s",
                    attempt,
                    redact_sensitive(str(exc)),
                )
                self._record_runtime_event(
                    "engine_recovery_failed",
                    attempt=attempt,
                    reason=reason,
                    error=redact_sensitive(str(exc)),
                )
                continue

            self._recovery_requested_reason = None
            self._last_bar_received_at = None
            logger.info("LiveEngine: Recovery succeeded on attempt %s", attempt)
            self._record_runtime_event(
                "engine_recovery_succeeded",
                attempt=attempt,
                reason=reason,
            )
            return True

        logger.error(
            "LiveEngine: Recovery failed after %s attempts; stopping",
            self.max_recovery_attempts,
        )
        self._record_runtime_event(
            "engine_recovery_exhausted",
            max_attempts=self.max_recovery_attempts,
            reason=reason,
        )
        self._shutdown_event.set()
        return False

    async def _cancel_watchdog(self) -> None:
        if self._watchdog_task is None:
            return
        self._watchdog_task.cancel()
        await asyncio.gather(self._watchdog_task, return_exceptions=True)
        self._watchdog_task = None

    async def stop(self) -> None:
        """Graceful shutdown."""
        logger.info("LiveEngine: Stopping...")
        self._shutdown_event.set()
        await self._cancel_watchdog()
        self.feed.stop()
        if self._run_in_progress:
            await self._run_done_event.wait()
        await self.broker.disconnect()
        logger.info("LiveEngine: Stopped")

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

    def _install_signal_handlers(self) -> None:
        """Install SIGINT/SIGTERM handlers for graceful shutdown."""
        loop = asyncio.get_running_loop()

        def handler(sig: signal.Signals) -> None:
            logger.info("LiveEngine: Received %s", sig.name)
            self._shutdown_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, handler, sig)
            except NotImplementedError:
                pass

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

    def _record_runtime_event(self, event: str, **payload: Any) -> None:
        """Forward runtime events into the broker journal when supported."""
        recorder = getattr(self.broker, "record_event", None)
        if callable(recorder):
            recorder(event, **payload)

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

        last_bar_age_seconds: float | None = None
        if self._last_bar_received_at is not None:
            last_bar_age_seconds = max(
                0.0,
                (reference_now - self._last_bar_received_at).total_seconds(),
            )

        if not self._running:
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
            "bar_count": self._bar_count,
            "error_count": self._error_count,
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
        }

    @property
    def stats(self) -> dict[str, Any]:
        """Get engine statistics and runtime health."""
        return self.runtime_status()
