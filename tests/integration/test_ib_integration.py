"""Integration tests for IBBroker with real IB connection.

REQUIREMENTS:
1. IB Gateway or TWS must be running
2. Paper trading mode (port 7497)
3. Socket client enabled in API settings
4. Read-Only API: OFF (to allow order submission in later tests)

To run these tests:
    pytest tests/integration/test_ib_integration.py -v

To skip if IB not available:
    pytest tests/integration/test_ib_integration.py -v --skip-integration
"""

import asyncio
from datetime import datetime

import pytest

from ml4t.backtest.types import Position
from ml4t.live.brokers.ib import IBBroker


@pytest.fixture
def ib_config():
    """IB connection configuration for paper trading."""
    return {
        'host': '127.0.0.1',
        'port': 7497,  # Paper trading
        'client_id': 999,  # High ID to avoid conflicts
    }


@pytest.fixture
async def ib_broker(ib_config):
    """Create and connect IBBroker for testing.

    Yields:
        Connected IBBroker instance

    Note:
        Automatically disconnects after test
    """
    broker = IBBroker(**ib_config)

    try:
        await broker.connect()
        yield broker
    finally:
        await broker.disconnect()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ib_connection(ib_config):
    """Test basic IB connection and disconnection.

    TASK-012: Verify connection setup from TASK-011 still works.
    """
    broker = IBBroker(**ib_config)

    # Should not be connected initially
    assert not broker.is_connected

    # Connect
    await broker.connect()
    assert broker.is_connected

    # Disconnect
    await broker.disconnect()
    assert not broker.is_connected


@pytest.mark.integration
@pytest.mark.asyncio
async def test_position_sync(ib_broker):
    """Test position synchronization from IB.

    TASK-012: Core acceptance criterion - verify _sync_positions() works.

    Note:
        This test requires positions to exist in the IB paper account.
        If no positions, it will pass but log a warning.
    """
    # Sync positions
    await ib_broker._sync_positions()

    # Get positions
    positions = ib_broker.positions

    # Verify positions is a dict
    assert isinstance(positions, dict)

    # If positions exist, verify structure
    if positions:
        for asset, pos in positions.items():
            assert isinstance(asset, str)
            assert isinstance(pos, Position)
            assert pos.asset == asset
            assert pos.quantity != 0  # IB doesn't report zero positions
            assert pos.entry_price >= 0.0
            assert isinstance(pos.entry_time, datetime)
    else:
        # No positions - this is fine for a clean paper account
        print("Warning: No positions found in IB paper account")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_position_access_thread_safe(ib_broker):
    """Test thread-safe position access.

    TASK-012: Verify shallow copy prevents race conditions (Gemini Critical Issue C).
    """
    await ib_broker._sync_positions()

    # Access via property (sync)
    positions_sync = ib_broker.positions
    assert isinstance(positions_sync, dict)

    # Access via async method
    positions_async = await ib_broker.get_positions_async()
    assert isinstance(positions_async, dict)

    # Both should have same keys
    assert set(positions_sync.keys()) == set(positions_async.keys())

    # Verify copies are independent (modifying one doesn't affect the other)
    if positions_sync:
        asset = list(positions_sync.keys())[0]
        original_qty = positions_sync[asset].quantity

        # Modify the copy
        positions_sync[asset] = Position(
            asset=asset,
            quantity=999.0,
            entry_price=100.0,
            entry_time=datetime.now(),
        )

        # Original should be unchanged
        current_positions = ib_broker.positions
        assert current_positions[asset].quantity == original_qty


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_position_single(ib_broker):
    """Test getting a single position.

    TASK-012: Verify get_position() method works correctly.
    """
    await ib_broker._sync_positions()

    positions = ib_broker.positions

    if positions:
        # Test existing position
        asset = list(positions.keys())[0]
        pos = ib_broker.get_position(asset)
        assert pos is not None
        assert pos.asset == asset

        # Test case insensitive
        pos_lower = ib_broker.get_position(asset.lower())
        assert pos_lower is not None
        assert pos_lower.asset == asset

    # Test non-existent position
    pos_none = ib_broker.get_position("NONEXISTENTSTOCK12345")
    assert pos_none is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_account_queries(ib_broker):
    """Test account value and cash queries.

    TASK-015: These methods are already implemented in TASK-011, verify they work.
    """
    # Get account value
    account_value = await ib_broker.get_account_value_async()
    assert isinstance(account_value, float)
    assert account_value >= 0.0

    # Get cash
    cash = await ib_broker.get_cash_async()
    assert isinstance(cash, float)
    assert cash >= 0.0

    # Paper account should have some value
    assert account_value > 0.0, "Paper account should have initial capital"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_position_callback(ib_broker):
    """Test position update via IB callback.

    TASK-012: Verify _on_position() callback correctly updates positions.

    Note:
        This test simulates a position update by creating a mock IB position object.
        Real position updates would come from IB events during trading.
    """
    # Create a mock IB position object
    class MockContract:
        def __init__(self, symbol):
            self.symbol = symbol

    class MockPosition:
        def __init__(self, symbol, quantity, avg_cost):
            self.contract = MockContract(symbol)
            self.position = quantity
            self.avgCost = avg_cost

    # Simulate position update
    mock_pos = MockPosition("TESTASSET", 100.0, 150.0)
    ib_broker._on_position(mock_pos)

    # Verify position was added
    pos = ib_broker.get_position("TESTASSET")
    assert pos is not None
    assert pos.asset == "TESTASSET"
    assert pos.quantity == 100.0
    assert pos.entry_price == 150.0

    # Simulate position close
    mock_pos_close = MockPosition("TESTASSET", 0, 0)
    ib_broker._on_position(mock_pos_close)

    # Verify position was removed
    pos_after = ib_broker.get_position("TESTASSET")
    assert pos_after is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_position_access(ib_broker):
    """Test concurrent position access doesn't cause race conditions.

    TASK-012: Stress test thread safety with concurrent reads.
    """
    await ib_broker._sync_positions()

    # Launch multiple concurrent position reads
    tasks = [
        ib_broker.get_positions_async()
        for _ in range(10)
    ]

    results = await asyncio.gather(*tasks)

    # All results should be identical
    first = results[0]
    for result in results[1:]:
        assert set(result.keys()) == set(first.keys())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ib_reconnection(ib_config):
    """Test disconnection and reconnection.

    TASK-012: Verify position sync works after reconnection.
    """
    broker = IBBroker(**ib_config)

    # First connection
    await broker.connect()
    await broker._sync_positions()
    positions_first = broker.positions

    # Disconnect
    await broker.disconnect()
    assert not broker.is_connected

    # Reconnect
    await broker.connect()
    assert broker.is_connected

    # Sync again
    await broker._sync_positions()
    positions_second = broker.positions

    # Positions should be consistent (assuming no trading happened)
    assert set(positions_first.keys()) == set(positions_second.keys())

    # Cleanup
    await broker.disconnect()


if __name__ == '__main__':
    """Run integration tests if IB is available."""
    pytest.main([__file__, '-v', '--tb=short'])
