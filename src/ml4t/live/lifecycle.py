"""Async orchestration for the versioned synchronous strategy lifecycle."""

from __future__ import annotations

import asyncio
import math
from collections import deque
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from time import monotonic
from typing import Any

from ml4t.specs import LIFECYCLE_V1, LifecycleContract, LifecyclePhase

RETAINED_CALLBACK_TRACE_LIMIT = 4_096


@dataclass(frozen=True, slots=True)
class LifecycleInvocation:
    """One retained live strategy callback invocation."""

    phase: LifecyclePhase
    callback: str
    event_time: datetime | None


class StrategyCallbackTimeoutError(TimeoutError):
    """Raised after an over-deadline callback becomes quiescent."""

    def __init__(self, callback: str, timeout_seconds: float, elapsed_seconds: float) -> None:
        self.callback = callback
        self.timeout_seconds = timeout_seconds
        self.elapsed_seconds = elapsed_seconds
        super().__init__(
            f"strategy callback {callback} exceeded {timeout_seconds:g}s "
            f"({elapsed_seconds:.3f}s elapsed)"
        )


class LiveLifecycleDispatcher:
    """Run every synchronous strategy callback on one dedicated worker thread."""

    def __init__(
        self,
        strategy: Any,
        contract: LifecycleContract = LIFECYCLE_V1,
        *,
        callback_timeout_seconds: float = 5.0,
        event_recorder: Callable[..., None] | None = None,
    ) -> None:
        if (
            isinstance(callback_timeout_seconds, bool)
            or not isinstance(callback_timeout_seconds, int | float)
            or not math.isfinite(callback_timeout_seconds)
            or callback_timeout_seconds <= 0
        ):
            raise ValueError("callback_timeout_seconds must be finite and positive")
        self.strategy = strategy
        self.contract = contract
        self.callback_timeout_seconds = float(callback_timeout_seconds)
        self.event_recorder = event_recorder
        self.invocations: deque[LifecycleInvocation] = deque(maxlen=RETAINED_CALLBACK_TRACE_LIMIT)
        self._invocation_count = 0
        self._counts = dict.fromkeys(LifecyclePhase, 0)
        self._executor: ThreadPoolExecutor | None = None

    @property
    def callback_counts(self) -> dict[LifecyclePhase, int]:
        """Return successful and failed invocation counts."""
        return dict(self._counts)

    @property
    def invocation_count(self) -> int:
        """Return the total invocation count, including pruned trace entries."""
        return self._invocation_count

    @property
    def dropped_invocation_count(self) -> int:
        """Return how many oldest trace entries were pruned from memory."""
        return max(0, self._invocation_count - len(self.invocations))

    async def dispatch(
        self,
        phase: LifecyclePhase,
        *args: Any,
        event_time: datetime | None = None,
    ) -> Any:
        """Invoke one contract callback without blocking or re-entering the event loop."""
        specification = self.contract.phase_spec(phase)
        callback = getattr(self.strategy, specification.callback)
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="ml4t-live-strategy",
            )
        self._counts[phase] += 1
        self._invocation_count += 1
        invocation = LifecycleInvocation(phase, specification.callback, event_time)
        self.invocations.append(invocation)
        self._record(
            "strategy_callback_started",
            forward=False,
            phase=phase.value,
            callback=specification.callback,
            event_time=event_time,
        )
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._executor, partial(callback, *args))
        started_at = monotonic()
        try:
            result = await asyncio.wait_for(
                asyncio.shield(future),
                timeout=self.callback_timeout_seconds,
            )
        except asyncio.CancelledError:
            try:
                await future
            except BaseException as error:
                self._record_failure(invocation, error)
            else:
                self._record(
                    "strategy_callback_succeeded",
                    forward=False,
                    phase=phase.value,
                    callback=specification.callback,
                    event_time=event_time,
                )
            raise
        except TimeoutError:
            try:
                await asyncio.shield(future)
            except BaseException as error:
                self._record_failure(invocation, error)
                raise
            timeout_error = StrategyCallbackTimeoutError(
                specification.callback,
                self.callback_timeout_seconds,
                monotonic() - started_at,
            )
            self._record_failure(invocation, timeout_error)
            raise timeout_error from None
        except BaseException as error:
            self._record_failure(invocation, error)
            raise
        self._record(
            "strategy_callback_succeeded",
            forward=False,
            phase=phase.value,
            callback=specification.callback,
            event_time=event_time,
        )
        return result

    def validate_completed_run(
        self,
        market_event_count: int,
        *,
        baseline: dict[LifecyclePhase, int] | None = None,
    ) -> None:
        """Validate exactly-once boundaries and ordinary market-event counts."""
        baseline = baseline or dict.fromkeys(LifecyclePhase, 0)
        for phase in (
            LifecyclePhase.RUN_START,
            LifecyclePhase.CAUSAL_INITIALIZATION,
            LifecyclePhase.RUN_END,
        ):
            self.contract.phase_spec(phase).validate_count(self._counts[phase] - baseline[phase])
        self.contract.phase_spec(LifecyclePhase.MARKET_EVENT).validate_count(
            self._counts[LifecyclePhase.MARKET_EVENT] - baseline[LifecyclePhase.MARKET_EVENT],
            event_count=market_event_count,
        )

    def close(self) -> None:
        """Release the dedicated callback worker after all callbacks finish."""
        if self._executor is None:
            return
        self._executor.shutdown(wait=True, cancel_futures=True)
        self._executor = None

    def _record_failure(self, invocation: LifecycleInvocation, error: BaseException) -> None:
        self._record(
            "strategy_callback_failed",
            phase=invocation.phase.value,
            callback=invocation.callback,
            event_time=invocation.event_time,
            error_type=type(error).__name__,
            error=str(error),
        )

    def _record(self, event: str, *, forward: bool = True, **payload: Any) -> None:
        if self.event_recorder is not None:
            self.event_recorder(event, forward=forward, **payload)


def callback_trace(
    invocations: Iterable[LifecycleInvocation],
) -> tuple[tuple[str, str, datetime | None], ...]:
    """Return a stable trace for cross-engine comparison."""
    return tuple(
        (invocation.phase.value, invocation.callback, invocation.event_time)
        for invocation in invocations
    )


__all__ = [
    "LifecycleInvocation",
    "LiveLifecycleDispatcher",
    "StrategyCallbackTimeoutError",
    "callback_trace",
]
