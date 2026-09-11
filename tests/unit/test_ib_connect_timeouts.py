"""Every await in IBBroker.connect() is bounded, including the post-handshake snapshot.

A Gateway can complete the API handshake and then stop answering requests. The handshake
was already bounded; `_sync_positions()` was not, so `reqPositionsAsync()` waited on a
reply that never came and `connect()` never returned, logged, or raised. These tests pin
both halves: the hang now fails, and a healthy connect still succeeds.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from ml4t.live.brokers import ib as ib_module
from ml4t.live.brokers.ib import IBBroker

ACCOUNT = "DU1234567"


def _vendor(*, positions_hang: bool) -> MagicMock:
    """A vendor that completes the handshake, then either answers or never does."""
    vendor = MagicMock()
    vendor.connectAsync = AsyncMock(return_value=None)
    vendor.managedAccounts = MagicMock(return_value=[ACCOUNT])
    vendor.openTrades = MagicMock(return_value=[])
    vendor.disconnect = MagicMock()
    if positions_hang:

        async def never_returns():
            await asyncio.Event().wait()  # the shape of a request the Gateway never answers

        vendor.reqPositionsAsync = never_returns
    else:
        vendor.reqPositionsAsync = AsyncMock(return_value=[])
    return vendor


@pytest.mark.asyncio
async def test_connect_fails_when_the_position_snapshot_never_answers(monkeypatch):
    monkeypatch.setattr(ib_module, "IB_SNAPSHOT_TIMEOUT", 0.2)
    broker = IBBroker(account=ACCOUNT)
    broker.ib = _vendor(positions_hang=True)

    started = asyncio.get_running_loop().time()
    with pytest.raises((TimeoutError, RuntimeError)):
        await asyncio.wait_for(broker.connect(), timeout=5)
    elapsed = asyncio.get_running_loop().time() - started

    # Without the bound this never returns; the 5s outer wait_for is the test's own
    # safety net and must not be what ends it.
    assert elapsed < 3, (
        f"connect() took {elapsed:.2f}s, so it was not the snapshot bound that ended it"
    )
    assert broker.is_connected is False
    broker.ib.disconnect.assert_called_once()


@pytest.mark.asyncio
async def test_connect_succeeds_when_the_snapshot_answers():
    broker = IBBroker(account=ACCOUNT)
    broker.ib = _vendor(positions_hang=False)
    broker.ib.isConnected = MagicMock(return_value=True)

    await asyncio.wait_for(broker.connect(), timeout=5)

    assert broker.is_connected is True
    broker.ib.disconnect.assert_not_called()
