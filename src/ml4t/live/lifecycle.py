"""Async orchestration for the versioned synchronous strategy lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any

from ml4t.specs import LIFECYCLE_V1, LifecycleContract, LifecyclePhase


@dataclass(frozen=True, slots=True)
class LifecycleInvocation:
    """One retained live strategy callback invocation."""

    phase: LifecyclePhase
    callback: str
    event_time: datetime | None


class LiveLifecycleDispatcher:
    """Run every synchronous strategy callback on one dedicated worker thread."""

    def __init__(
        self,
        strategy: Any,
        contract: LifecycleContract = LIFECYCLE_V1,
        *,
        event_recorder: Callable[..., None] | None = None,
    ) -> None:
        self.strategy = strategy
        self.contract = contract
        self.event_recorder = event_recorder
        self.invocations: list[LifecycleInvocation] = []
        self._counts = dict.fromkeys(LifecyclePhase, 0)
        self._executor: ThreadPoolExecutor | None = None

    @property
    def callback_counts(self) -> dict[LifecyclePhase, int]:
        """Return successful and failed invocation counts."""
        return dict(self._counts)

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
        invocation = LifecycleInvocation(phase, specification.callback, event_time)
        self.invocations.append(invocation)
        self._record(
            "strategy_callback_started",
            phase=phase.value,
            callback=specification.callback,
            event_time=event_time,
        )
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._executor, partial(callback, *args))
        try:
            result = await asyncio.shield(future)
        except asyncio.CancelledError:
            try:
                await future
            except BaseException as error:
                self._record_failure(invocation, error)
            else:
                self._record(
                    "strategy_callback_succeeded",
                    phase=phase.value,
                    callback=specification.callback,
                    event_time=event_time,
                )
            raise
        except BaseException as error:
            self._record_failure(invocation, error)
            raise
        self._record(
            "strategy_callback_succeeded",
            phase=phase.value,
            callback=specification.callback,
            event_time=event_time,
        )
        return result

    def validate_completed_run(self, market_event_count: int) -> None:
        """Validate exactly-once boundaries and ordinary market-event counts."""
        for phase in (
            LifecyclePhase.RUN_START,
            LifecyclePhase.CAUSAL_INITIALIZATION,
            LifecyclePhase.RUN_END,
        ):
            self.contract.phase_spec(phase).validate_count(self._counts[phase])
        self.contract.phase_spec(LifecyclePhase.MARKET_EVENT).validate_count(
            self._counts[LifecyclePhase.MARKET_EVENT],
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

    def _record(self, event: str, **payload: Any) -> None:
        if self.event_recorder is not None:
            self.event_recorder(event, **payload)


def callback_trace(
    invocations: Sequence[LifecycleInvocation],
) -> tuple[tuple[str, str, datetime | None], ...]:
    """Return a stable trace for cross-engine comparison."""
    return tuple(
        (invocation.phase.value, invocation.callback, invocation.event_time)
        for invocation in invocations
    )


__all__ = ["LifecycleInvocation", "LiveLifecycleDispatcher", "callback_trace"]
