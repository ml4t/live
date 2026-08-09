"""Tests for the performance evidence acceptance rules."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from scripts.qualification.qualify_performance import (
    ORDER_EVENTS,
    QUEUE_CAPACITY,
    RSS_GROWTH_LIMIT_BYTES,
    validate_report,
)


def passing_report() -> dict[str, Any]:
    run = {
        "events": 2,
        "latency_sample_count": 1,
        "event_checksum": "event",
        "produced_event_checksum": "event",
        "intent_checksum": "intent",
        "throughput_events_per_second": 101.0,
        "dispatch_latency_ms": {"p99": 9.0},
        "rss_growth_bytes": RSS_GROWTH_LIMIT_BYTES - 1,
        "shutdown_seconds": 4.0,
        "continuity": {"violation_count": 0},
        "diagnostics": {
            "callback_invocations_retained": 4_096,
            "operational_events_retained": 4_096,
            "operational_events_forwarded": 7,
        },
        "audit_event_count": 7,
    }
    return {
        "workloads": {
            "sustained": {
                "configuration": {"events": 2, "warmup_events": 1},
                "runs": [deepcopy(run), deepcopy(run)],
            },
            "idle": {"events": 0, "shutdown_seconds": 4.0},
            "slow_strategy": {
                "events": QUEUE_CAPACITY,
                "queue": {"high_watermark": QUEUE_CAPACITY},
            },
            "burst_overload": {
                "events_dispatched": 0,
                "error_type": "FeedOverflowError",
                "queue": {"overflow_count": 1, "occupancy": 0},
                "safety_event_count": 1,
            },
            "high_order_rate": {
                "events": ORDER_EVENTS,
                "orders": ORDER_EVENTS,
                "order_checksum": "order",
                "expected_order_checksum": "order",
                "event_checksum": "order-event",
                "produced_event_checksum": "order-event",
            },
            "reconnect": {
                "events": 2,
                "feed_start_count": 2,
                "recovery_attempts": 1,
                "recovery_event_count": 1,
                "continuity": {"violation_count": 0},
                "runtime_state": "stopped",
            },
        }
    }


def test_passing_performance_report_satisfies_every_acceptance_rule() -> None:
    assert validate_report(passing_report()) == []


@pytest.mark.parametrize(
    ("path", "value", "expected"),
    [
        (("sustained", "runs", 0, "produced_event_checksum"), "changed", "checksum"),
        (
            ("sustained", "runs", 0, "rss_growth_bytes"),
            RSS_GROWTH_LIMIT_BYTES,
            "RSS",
        ),
        (("sustained", "runs", 0, "audit_event_count"), 6, "diagnostic"),
        (("burst_overload", "error_type"), None, "overload"),
        (("high_order_rate", "order_checksum"), "changed", "high-order-rate"),
        (("reconnect", "recovery_attempts"), 2, "reconnect"),
    ],
)
def test_seeded_performance_fault_is_rejected(
    path: tuple[str | int, ...],
    value: Any,
    expected: str,
) -> None:
    report = passing_report()
    target: Any = report["workloads"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    assert any(expected in failure for failure in validate_report(report))
