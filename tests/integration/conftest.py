"""Pytest configuration for integration tests.

Applies ib_async.util.patchAsyncio() for nested event loop support.

Critical fix for pytest: Clear util.getLoop() cache before each test to prevent
"Event loop is closed" errors. See: https://github.com/ib-api-reloaded/ib_async/issues/186
"""

import asyncio
import pytest
from ib_async import util

from ml4t.live.brokers.ib import IBBroker

# Patch asyncio to allow nested event loops
util.patchAsyncio()


@pytest.fixture
async def ib_broker(request):
    """Provide a connected IB broker with proper cleanup.

    Uses test name hash for unique client ID to avoid conflicts.
    Follows ib_insync best practices for pytest integration.

    CRITICAL: Clears util.getLoop() cache to prevent closed loop errors.
    The @functools.cache on getLoop() persists closed loops across tests.

    Args:
        request: Pytest request object for test metadata

    Yields:
        Connected IBBroker instance
    """
    # Clear cached event loop (fixes "Event loop is closed" error)
    # See: https://github.com/ib-api-reloaded/ib_async/issues/186
    if hasattr(util.getLoop, 'cache_clear'):
        util.getLoop.cache_clear()

    # Generate unique client ID from test name
    client_id = hash(request.node.name) % 1000 + 1000  # Range: 1000-1999

    broker = IBBroker(
        host="127.0.0.1",
        port=7497,
        client_id=client_id,
    )

    await broker.connect()
    yield broker

    # Clean disconnect - note: disconnect() is sync, not async!
    broker.ib.disconnect()
    await asyncio.sleep(0.3)  # Allow TWS to clean up socket
