"""Unit tests for BarBuffer."""

from ml4t.live.feeds.aggregator import BarBuffer


class TestBarBuffer:
    """Test suite for BarBuffer tick aggregation."""

    def test_initial_state(self):
        """Test BarBuffer starts with correct initial state."""
        bar = BarBuffer()
        assert bar.open is None
        assert bar.high == float('-inf')
        assert bar.low == float('inf')
        assert bar.close == 0.0
        assert bar.volume == 0

    def test_single_tick_update(self):
        """Test updating with a single tick."""
        bar = BarBuffer()
        bar.update(100.0, size=10)

        assert bar.open == 100.0
        assert bar.high == 100.0
        assert bar.low == 100.0
        assert bar.close == 100.0
        assert bar.volume == 10

    def test_multiple_ticks_ohlc(self):
        """Test OHLC tracking with multiple ticks."""
        bar = BarBuffer()
        bar.update(100.0, size=10)  # Open
        bar.update(105.0, size=5)   # New high
        bar.update(95.0, size=15)   # New low
        bar.update(102.0, size=8)   # Close

        assert bar.open == 100.0
        assert bar.high == 105.0
        assert bar.low == 95.0
        assert bar.close == 102.0
        assert bar.volume == 38

    def test_volume_accumulation(self):
        """Test volume accumulates correctly."""
        bar = BarBuffer()
        bar.update(100.0, size=10)
        bar.update(101.0, size=20)
        bar.update(102.0, size=30)

        assert bar.volume == 60

    def test_update_without_size(self):
        """Test update with quote ticks (no size)."""
        bar = BarBuffer()
        bar.update(100.0)  # No size argument
        bar.update(101.0)

        assert bar.open == 100.0
        assert bar.close == 101.0
        assert bar.volume == 0  # No volume from quote ticks

    def test_to_dict_with_ticks(self):
        """Test to_dict() serialization with normal ticks."""
        bar = BarBuffer()
        bar.update(100.0, size=10)
        bar.update(105.0, size=5)
        bar.update(95.0, size=15)
        bar.update(102.0, size=8)

        result = bar.to_dict()
        assert result == {
            'open': 100.0,
            'high': 105.0,
            'low': 95.0,
            'close': 102.0,
            'volume': 38,
        }

    def test_to_dict_no_ticks(self):
        """Test to_dict() with no ticks received (uses close as fallback)."""
        bar = BarBuffer()
        result = bar.to_dict()

        # Should use close (0.0) as fallback for all OHLC
        assert result == {
            'open': 0.0,
            'high': 0.0,
            'low': 0.0,
            'close': 0.0,
            'volume': 0,
        }

    def test_to_dict_single_tick(self):
        """Test to_dict() with single tick."""
        bar = BarBuffer()
        bar.update(100.0, size=10)

        result = bar.to_dict()
        assert result == {
            'open': 100.0,
            'high': 100.0,
            'low': 100.0,
            'close': 100.0,
            'volume': 10,
        }

    def test_reset(self):
        """Test reset() clears all state."""
        bar = BarBuffer()
        bar.update(100.0, size=10)
        bar.update(105.0, size=5)

        bar.reset()

        assert bar.open is None
        assert bar.high == float('-inf')
        assert bar.low == float('inf')
        assert bar.close == 0.0
        assert bar.volume == 0

    def test_reset_and_reuse(self):
        """Test BarBuffer can be reused after reset."""
        bar = BarBuffer()
        
        # First bar
        bar.update(100.0, size=10)
        bar.update(105.0, size=5)
        first_bar = bar.to_dict()
        
        # Reset and create second bar
        bar.reset()
        bar.update(200.0, size=20)
        bar.update(210.0, size=10)
        second_bar = bar.to_dict()

        # Verify first bar
        assert first_bar == {
            'open': 100.0,
            'high': 105.0,
            'low': 100.0,
            'close': 105.0,
            'volume': 15,
        }

        # Verify second bar (independent of first)
        assert second_bar == {
            'open': 200.0,
            'high': 210.0,
            'low': 200.0,
            'close': 210.0,
            'volume': 30,
        }

    def test_high_tracking(self):
        """Test high price is always maximum."""
        bar = BarBuffer()
        bar.update(100.0)
        bar.update(110.0)  # New high
        bar.update(105.0)  # Lower than high
        bar.update(108.0)  # Still lower than high

        assert bar.high == 110.0

    def test_low_tracking(self):
        """Test low price is always minimum."""
        bar = BarBuffer()
        bar.update(100.0)
        bar.update(95.0)   # New low
        bar.update(98.0)   # Higher than low
        bar.update(97.0)   # Still higher than low

        assert bar.low == 95.0

    def test_close_updates_every_tick(self):
        """Test close is always the most recent price."""
        bar = BarBuffer()
        bar.update(100.0)
        assert bar.close == 100.0

        bar.update(105.0)
        assert bar.close == 105.0

        bar.update(95.0)
        assert bar.close == 95.0

        bar.update(102.0)
        assert bar.close == 102.0

    def test_identical_prices(self):
        """Test bar with all identical prices."""
        bar = BarBuffer()
        bar.update(100.0, size=10)
        bar.update(100.0, size=20)
        bar.update(100.0, size=30)

        assert bar.open == 100.0
        assert bar.high == 100.0
        assert bar.low == 100.0
        assert bar.close == 100.0
        assert bar.volume == 60

    def test_large_volume(self):
        """Test volume accumulation with large numbers."""
        bar = BarBuffer()
        bar.update(100.0, size=1_000_000)
        bar.update(101.0, size=2_000_000)
        bar.update(102.0, size=3_000_000)

        assert bar.volume == 6_000_000

    def test_zero_volume_ticks(self):
        """Test ticks with zero volume (quote updates)."""
        bar = BarBuffer()
        bar.update(100.0, size=0)
        bar.update(101.0, size=0)
        bar.update(102.0, size=0)

        assert bar.open == 100.0
        assert bar.close == 102.0
        assert bar.volume == 0

    def test_mixed_trade_and_quote_ticks(self):
        """Test mixing trade ticks (with volume) and quote ticks (no volume)."""
        bar = BarBuffer()
        bar.update(100.0, size=10)  # Trade
        bar.update(100.5, size=0)   # Quote
        bar.update(101.0, size=20)  # Trade
        bar.update(100.8, size=0)   # Quote
        bar.update(102.0, size=5)   # Trade

        assert bar.open == 100.0
        assert bar.high == 102.0
        assert bar.low == 100.0
        assert bar.close == 102.0
        assert bar.volume == 35  # Only trades count

    def test_descending_prices(self):
        """Test bar with descending prices."""
        bar = BarBuffer()
        bar.update(110.0, size=10)  # Open (high)
        bar.update(105.0, size=5)
        bar.update(100.0, size=15)
        bar.update(95.0, size=8)    # Close (low)

        assert bar.open == 110.0
        assert bar.high == 110.0
        assert bar.low == 95.0
        assert bar.close == 95.0
        assert bar.volume == 38

    def test_ascending_prices(self):
        """Test bar with ascending prices."""
        bar = BarBuffer()
        bar.update(95.0, size=10)   # Open (low)
        bar.update(100.0, size=5)
        bar.update(105.0, size=15)
        bar.update(110.0, size=8)   # Close (high)

        assert bar.open == 95.0
        assert bar.high == 110.0
        assert bar.low == 95.0
        assert bar.close == 110.0
        assert bar.volume == 38
