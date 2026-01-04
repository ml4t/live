"""DataBento market data feed.

Provides historical and real-time market data from DataBento.

Features:
- Historical bar replay
- Real-time streaming
- Multiple datasets (MBO, MBP, OHLCV, Trades)
- Time-based replay for backtesting

Example Historical Replay:
    feed = DataBentoFeed.from_file(
        'path/to/databento.dbn',
        symbols=['SPY', 'QQQ'],
    )
    await feed.start()

Example Real-time:
    feed = DataBentoFeed.from_live(
        api_key='your-key',
        dataset='GLBX.MDP3',
        symbols=['ES.FUT', 'NQ.FUT'],
    )
    await feed.start()
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Any

from ml4t.live.protocols import DataFeedProtocol

logger = logging.getLogger(__name__)

# DataBento is optional dependency
try:
    import databento as db
    DATABENTO_AVAILABLE = True
except ImportError:
    DATABENTO_AVAILABLE = False
    logger.warning("databento package not installed - DataBentoFeed unavailable")


class DataBentoFeed(DataFeedProtocol):
    """Market data feed from DataBento.

    Supports both historical replay and real-time streaming.

    Historical Mode:
        - Reads from .dbn files (DataBento native format)
        - Replays at historical speed or accelerated
        - Perfect for strategy validation

    Real-time Mode:
        - Streams live market data
        - Supports multiple datasets (GLBX, XNAS, OPRA, etc.)
        - Low-latency tick data

    Data Format:
        timestamp: datetime - Event timestamp
        data: dict[str, dict] - {symbol: {'open', 'high', 'low', 'close', 'volume'}}
        context: dict - Additional fields (bid, ask, trade_count, etc.)

    Example Historical:
        feed = DataBentoFeed.from_file(
            'ES_202401.dbn',
            symbols=['ES.FUT'],
            replay_speed=10.0,  # 10x speed
        )

    Example Real-time:
        feed = DataBentoFeed.from_live(
            api_key=os.getenv('DATABENTO_API_KEY'),
            dataset='GLBX.MDP3',
            schema='ohlcv-1s',
            symbols=['ES.c.0', 'NQ.c.0'],
        )
    """

    def __init__(
        self,
        client: 'db.Historical | db.Live',
        symbols: list[str],
        *,
        mode: str = 'historical',
        replay_speed: float = 1.0,
    ):
        """Initialize DataBento feed.

        Args:
            client: DataBento client (Historical or Live)
            symbols: List of symbols to subscribe to
            mode: 'historical' or 'live'
            replay_speed: Playback speed multiplier (historical only)
                1.0 = real-time, 10.0 = 10x speed
        """
        if not DATABENTO_AVAILABLE:
            raise ImportError(
                "databento package required. Install with: pip install databento"
            )

        self.client = client
        self.symbols = symbols
        self.mode = mode
        self.replay_speed = replay_speed

        # State
        self._queue: asyncio.Queue = asyncio.Queue()
        self._running = False
        self._replay_task: asyncio.Task | None = None

        # Statistics
        self._record_count = 0

    @classmethod
    def from_file(
        cls,
        file_path: str | Path,
        symbols: list[str],
        *,
        replay_speed: float = 1.0,
    ) -> 'DataBentoFeed':
        """Create feed from historical .dbn file.

        Args:
            file_path: Path to .dbn file
            symbols: Symbols to filter (or all if empty)
            replay_speed: Playback speed (1.0 = real-time)

        Returns:
            DataBentoFeed configured for historical replay
        """
        if not DATABENTO_AVAILABLE:
            raise ImportError("databento package not installed")

        # Read file
        store = db.DBNStore.from_file(file_path)

        return cls(
            client=store,
            symbols=symbols,
            mode='historical',
            replay_speed=replay_speed,
        )

    @classmethod
    def from_live(
        cls,
        api_key: str,
        dataset: str,
        schema: str,
        symbols: list[str],
    ) -> 'DataBentoFeed':
        """Create feed for real-time streaming.

        Args:
            api_key: DataBento API key
            dataset: Dataset code (e.g., 'GLBX.MDP3', 'XNAS.ITCH')
            schema: Data schema (e.g., 'ohlcv-1s', 'mbp-10', 'trades')
            symbols: Symbols to subscribe to

        Returns:
            DataBentoFeed configured for live streaming
        """
        if not DATABENTO_AVAILABLE:
            raise ImportError("databento package not installed")

        client = db.Live(key=api_key)

        # Configure subscription
        client.subscribe(
            dataset=dataset,
            schema=schema,
            symbols=symbols,
        )

        return cls(
            client=client,
            symbols=symbols,
            mode='live',
        )

    async def start(self) -> None:
        """Start data feed.

        Historical mode: Begins replay task
        Live mode: Starts streaming subscription
        """
        logger.info(f"DataBentoFeed: Starting {self.mode} feed for {len(self.symbols)} symbols")
        self._running = True

        if self.mode == 'historical':
            # Start replay task
            self._replay_task = asyncio.create_task(self._replay_historical())
        elif self.mode == 'live':
            # Start live streaming task
            self._replay_task = asyncio.create_task(self._stream_live())

        logger.info("DataBentoFeed: Feed started")

    def stop(self) -> None:
        """Stop data feed."""
        logger.info("DataBentoFeed: Stopping feed")
        self._running = False

        # Cancel replay task
        if self._replay_task:
            self._replay_task.cancel()

        # Signal consumer
        self._queue.put_nowait(None)

        logger.info(f"DataBentoFeed: Stopped. Records: {self._record_count}")

    async def _replay_historical(self) -> None:
        """Replay historical data from DBN file.

        Respects original timing with replay_speed multiplier.
        """
        last_timestamp: datetime | None = None

        for record in self.client:
            if not self._running:
                break

            # Convert to our format
            timestamp, data, context = self._convert_record(record)

            # Time-based replay (sleep between records)
            if last_timestamp and self.replay_speed > 0:
                time_delta = (timestamp - last_timestamp).total_seconds()
                sleep_time = time_delta / self.replay_speed
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)

            last_timestamp = timestamp
            self._record_count += 1

            # Emit data
            self._queue.put_nowait((timestamp, data, context))

        # End of data
        self.stop()

    async def _stream_live(self) -> None:
        """Stream real-time data from DataBento."""
        async for record in self.client:
            if not self._running:
                break

            # Convert to our format
            timestamp, data, context = self._convert_record(record)
            self._record_count += 1

            # Emit data
            self._queue.put_nowait((timestamp, data, context))

    def _convert_record(self, record) -> tuple[datetime, dict, dict]:
        """Convert DataBento record to our format.

        Args:
            record: DataBento record (OHLCV, MBP, Trade, etc.)

        Returns:
            Tuple of (timestamp, data, context)
        """
        # Extract timestamp
        timestamp = datetime.fromtimestamp(record.ts_event / 1e9)  # nanoseconds

        # Extract symbol
        symbol = record.symbol if hasattr(record, 'symbol') else 'UNKNOWN'

        # Build data dict based on schema
        data = {}
        context = {}

        if hasattr(record, 'open'):  # OHLCV record
            data[symbol] = {
                'open': float(record.open) / 1e9 if record.open else None,
                'high': float(record.high) / 1e9 if record.high else None,
                'low': float(record.low) / 1e9 if record.low else None,
                'close': float(record.close) / 1e9 if record.close else None,
                'volume': int(record.volume) if record.volume else 0,
            }
        elif hasattr(record, 'price'):  # Trade record
            data[symbol] = {
                'price': float(record.price) / 1e9,
                'size': int(record.size),
            }
        elif hasattr(record, 'bid_px_00'):  # MBP record
            data[symbol] = {
                'price': float(record.bid_px_00) / 1e9,  # Use best bid as price
                'size': int(record.bid_sz_00),
            }
            context[symbol] = {
                'bid': float(record.bid_px_00) / 1e9,
                'ask': float(record.ask_px_00) / 1e9,
                'bid_size': int(record.bid_sz_00),
                'ask_size': int(record.ask_sz_00),
            }

        return timestamp, data, context

    async def __aiter__(self) -> AsyncIterator[tuple[datetime, dict, dict]]:
        """Async iterator yielding market data.

        Yields:
            Tuple of (timestamp, data, context)
        """
        while self._running:
            item = await self._queue.get()

            if item is None:  # Shutdown sentinel
                break

            yield item

    @property
    def stats(self) -> dict[str, Any]:
        """Get feed statistics."""
        return {
            'running': self._running,
            'mode': self.mode,
            'record_count': self._record_count,
            'symbols': self.symbols,
            'replay_speed': self.replay_speed,
        }
