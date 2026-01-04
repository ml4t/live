"""Pytest configuration and shared fixtures."""

import pytest


@pytest.fixture
def mock_ib_connection():
    """Mock IB connection for testing without real broker."""
    # Will be implemented in integration tests
    pass


@pytest.fixture
def sample_strategy():
    """Sample strategy for testing."""
    # Will be implemented when Strategy is available
    pass
