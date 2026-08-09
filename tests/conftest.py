"""Qualification marker controls shared by every test suite."""

from __future__ import annotations

import asyncio

import pytest


def pytest_sessionstart(session: pytest.Session) -> None:
    asyncio.set_event_loop(None)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-external",
        action="store_true",
        default=False,
        help="run tests that require a network service, process, or paper credentials",
    )
    parser.addoption(
        "--run-stress",
        action="store_true",
        default=False,
        help="run sustained resource and overload tests",
    )
    parser.addoption(
        "--run-benchmarks",
        action="store_true",
        default=False,
        help="run performance measurements",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    controls = (
        (
            "external",
            "--run-external",
            "external test requires --run-external and its documented service or credentials",
        ),
        ("stress", "--run-stress", "stress test requires --run-stress"),
        ("benchmark", "--run-benchmarks", "benchmark requires --run-benchmarks"),
    )
    for marker, option, reason in controls:
        if config.getoption(option):
            continue
        skip = pytest.mark.skip(reason=reason)
        for item in items:
            if item.get_closest_marker(marker) is not None:
                item.add_marker(skip)
