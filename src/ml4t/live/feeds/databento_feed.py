"""Experimental DataBento market data feed.

This adapter is not part of the beta support contract. Its selected deterministic schema adapters
do not qualify the DataBento service, datasets, or live performance.

Features:
- Historical bar replay
- Real-time streaming
- Multiple datasets (MBO, MBP, OHLCV, Trades)
- Time-based replay for backtesting

Example Historical Replay:
    feed = DataBentoFeed.from_file(
        'path/to/databento.dbn',
        symbols=['SPY', 'QQQ'],
        experimental=True,
    )
    await feed.start()

Example Real-time:
    feed = DataBentoFeed.from_live(
        api_key='your-key',
        dataset='GLBX.MDP3',
        symbols=['ES.FUT', 'NQ.FUT'],
        experimental=True,
    )
    await feed.start()
"""

import asyncio
import logging
import math
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from ml4t.specs import (
    BarPayload,
    EventCompletion,
    LifecycleVersion,
    MarketEvent,
    MarketEventKind,
    QuotePayload,
    TradePayload,
)

from ml4t.live.feeds.events import FeedContractError, sequence_unavailable
from ml4t.live.feeds.experimental import require_experimental_opt_in
from ml4t.live.protocols import DataFeedProtocol

logger = logging.getLogger(__name__)

# DataBento is an optional dependency.
db: Any = None
try:
    import databento as db  # type: ignore[import-unresolved]

    DATABENTO_AVAILABLE = True
except ImportError:
    DATABENTO_AVAILABLE = False


DATABENTO_MISSING_GUARANTEES = (
    "bounded overload behavior",
    "schema-wide causal qualification",
    "credentialed live-service qualification",
)


def _validate_symbols(symbols: list[str]) -> None:
    if not symbols or any(not isinstance(symbol, str) or not symbol.strip() for symbol in symbols):
        raise ValueError("symbols must contain at least one non-empty symbol")


def _validate_replay_speed(replay_speed: float) -> None:
    if (
        isinstance(replay_speed, bool)
        or not isinstance(replay_speed, int | float)
        or not math.isfinite(replay_speed)
        or replay_speed < 0
    ):
        raise ValueError("replay_speed must be finite and non-negative")


class DataBentoFeed(DataFeedProtocol):
    """Experimental market data feed from DataBento.

    Supports selected historical replay and live record shapes after explicit opt-in.

    Historical Mode:
        - Reads from .dbn files (DataBento native format)
        - Replays at historical speed or accelerated
        - Intended for custom evaluation under the experimental limitations

    Real-time Mode:
        - Streams live market data
        - Supports multiple datasets (GLBX, XNAS, OPRA, etc.)
        - No beta latency or throughput guarantee

    Data Format:
        Experimental typed ``MarketEvent`` bars, trades, and quotes with UTC timestamps.

    Example Historical:
        feed = DataBentoFeed.from_file(
            'ES_202401.dbn',
            symbols=['ES.FUT'],
            replay_speed=10.0,  # 10x speed
            experimental=True,
        )

    Example Real-time:
        feed = DataBentoFeed.from_live(
            api_key=os.getenv('DATABENTO_API_KEY'),
            dataset='GLBX.MDP3',
            schema='ohlcv-1s',
            symbols=['ES.c.0', 'NQ.c.0'],
            experimental=True,
        )
    """

    support_status: ClassVar[str] = "experimental"

    def __init__(
        self,
        client: "db.Historical | db.Live",
        symbols: list[str],
        *,
        mode: str = "historical",
        replay_speed: float = 1.0,
        experimental: bool = False,
    ):
        """Initialize DataBento feed.

        Args:
            client: DataBento client (Historical or Live)
            symbols: List of symbols to subscribe to
            mode: 'historical' or 'live'
            replay_speed: Playback speed multiplier (historical only)
                1.0 = real-time, 10.0 = 10x speed
            experimental: Must be true to acknowledge the unsupported feed contract.
        """
        require_experimental_opt_in(
            "DataBentoFeed",
            experimental=experimental,
            missing_guarantees=DATABENTO_MISSING_GUARANTEES,
        )
        if not DATABENTO_AVAILABLE:
            raise ImportError("databento package required. Install with: pip install databento")
        _validate_symbols(symbols)
        if mode not in {"historical", "live"}:
            raise ValueError("mode must be 'historical' or 'live'")
        _validate_replay_speed(replay_speed)

        self.client = client
        self.symbols = list(symbols)
        self.mode = mode
        self.replay_speed = replay_speed

        # State
        self._queue: asyncio.Queue[MarketEvent | None] = asyncio.Queue()
        self._running = False
        self._replay_task: asyncio.Task | None = None
        self._failure: RuntimeError | None = None

        # Statistics
        self._record_count = 0

    @classmethod
    def from_file(
        cls,
        file_path: str | Path,
        symbols: list[str],
        *,
        replay_speed: float = 1.0,
        experimental: bool = False,
    ) -> "DataBentoFeed":
        """Create feed from historical .dbn file.

        Args:
            file_path: Path to .dbn file
            symbols: Non-empty symbol selection
            replay_speed: Playback speed (1.0 = real-time)
            experimental: Must be true to acknowledge the unsupported feed contract.

        Returns:
            DataBentoFeed configured for historical replay
        """
        if experimental is not True:
            require_experimental_opt_in(
                "DataBentoFeed",
                experimental=experimental,
                missing_guarantees=DATABENTO_MISSING_GUARANTEES,
            )
        if not DATABENTO_AVAILABLE:
            raise ImportError("databento package not installed")
        _validate_symbols(symbols)
        _validate_replay_speed(replay_speed)

        # Read file
        store = db.DBNStore.from_file(file_path)

        return cls(
            client=store,
            symbols=symbols,
            mode="historical",
            replay_speed=replay_speed,
            experimental=experimental,
        )

    @classmethod
    def from_live(
        cls,
        api_key: str,
        dataset: str,
        schema: str,
        symbols: list[str],
        *,
        experimental: bool = False,
    ) -> "DataBentoFeed":
        """Create feed for real-time streaming.

        Args:
            api_key: DataBento API key
            dataset: Dataset code (e.g., 'GLBX.MDP3', 'XNAS.ITCH')
            schema: Data schema (e.g., 'ohlcv-1s', 'mbp-10', 'trades')
            symbols: Symbols to subscribe to
            experimental: Must be true to acknowledge the unsupported feed contract.

        Returns:
            DataBentoFeed configured for live streaming
        """
        if experimental is not True:
            require_experimental_opt_in(
                "DataBentoFeed",
                experimental=experimental,
                missing_guarantees=DATABENTO_MISSING_GUARANTEES,
            )
        if not DATABENTO_AVAILABLE:
            raise ImportError("databento package not installed")
        _validate_symbols(symbols)

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
            mode="live",
            experimental=experimental,
        )

    async def start(self) -> None:
        """Start data feed.

        Historical mode: Begins replay task
        Live mode: Starts streaming subscription
        """
        if self._running:
            return
        logger.info(f"DataBentoFeed: Starting {self.mode} feed for {len(self.symbols)} symbols")
        self._queue = asyncio.Queue()
        self._failure = None
        self._running = True

        if self.mode == "historical":
            # Start replay task
            self._replay_task = asyncio.create_task(self._replay_historical())
        elif self.mode == "live":
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

        self._signal_stop()

        logger.info(f"DataBentoFeed: Stopped. Records: {self._record_count}")

    async def _replay_historical(self) -> None:
        """Replay historical data from DBN file.

        Respects original timing with replay_speed multiplier.
        """
        last_timestamp: datetime | None = None

        try:
            for record in self.client:
                if not self._running:
                    break

                # Convert to our format
                event = self._convert_record(record)

                # Time-based replay (sleep between records)
                if last_timestamp and self.replay_speed > 0:
                    time_delta = (event.event_time - last_timestamp).total_seconds()
                    sleep_time = time_delta / self.replay_speed
                    if sleep_time > 0:
                        await asyncio.sleep(sleep_time)

                last_timestamp = event.event_time
                self._record_count += 1

                # Emit data
                self._queue.put_nowait(event)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._stream_failed(error, stream="historical replay")
        finally:
            if self._running:
                self._running = False
                self._signal_stop()
            self._replay_task = None

    async def _stream_live(self) -> None:
        """Stream real-time data from DataBento."""
        try:
            async for record in self.client:
                if not self._running:
                    break

                # Convert to our format
                event = self._convert_record(record)
                self._record_count += 1

                # Emit data
                self._queue.put_nowait(event)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._stream_failed(error, stream="live")
        finally:
            if self._running:
                self._running = False
                self._signal_stop()
            self._replay_task = None

    def _convert_record(self, record: Any) -> MarketEvent:
        """Convert DataBento record to our format.

        Args:
            record: DataBento record (OHLCV, MBP, Trade, etc.)

        Returns:
            Typed experimental market event.
        """
        timestamp = datetime.fromtimestamp(record.ts_event / 1e9, tz=UTC)

        symbol = getattr(record, "symbol", None)
        if not isinstance(symbol, str) or not symbol.strip():
            raise FeedContractError("DataBento record must contain a non-empty symbol")

        if hasattr(record, "open"):  # OHLCV record
            kind = MarketEventKind.BAR
            completion = EventCompletion.COMPLETE
            payload = BarPayload(
                float(record.open) / 1e9,
                float(record.high) / 1e9,
                float(record.low) / 1e9,
                float(record.close) / 1e9,
                float(record.volume),
            )
        elif hasattr(record, "bid_px_00"):  # MBP record
            kind = MarketEventKind.QUOTE
            completion = EventCompletion.EVOLVING
            payload = QuotePayload(
                float(record.bid_px_00) / 1e9,
                float(record.ask_px_00) / 1e9,
                float(record.bid_sz_00),
                float(record.ask_sz_00),
            )
        elif hasattr(record, "price"):  # Trade record
            kind = MarketEventKind.TRADE
            completion = EventCompletion.COMPLETE
            payload = TradePayload(float(record.price) / 1e9, float(record.size))
        else:
            raise FeedContractError(f"unsupported DataBento record schema: {type(record).__name__}")

        provider_sequence = getattr(record, "sequence", None)
        if provider_sequence is not None and (
            isinstance(provider_sequence, bool) or not isinstance(provider_sequence, str | int)
        ):
            provider_sequence = str(provider_sequence)
        return MarketEvent(
            version=LifecycleVersion.V1,
            event_time=timestamp,
            receipt_time=datetime.now(UTC),
            kind=kind,
            completion=completion,
            source="databento",
            asset=symbol,
            payload=payload,
            provider_sequence=provider_sequence,
            gap=(
                sequence_unavailable("DataBento", type(record).__name__)
                if provider_sequence is None
                else None
            ),
            metadata={
                "experimental": True,
                "mode": self.mode,
                "schema": type(record).__name__,
            },
        )

    async def __aiter__(self) -> AsyncIterator[MarketEvent]:
        """Async iterator yielding market data.

        Yields:
            Typed experimental market event.
        """
        while True:
            item = await self._queue.get()

            if item is None:  # Shutdown sentinel
                if self._failure is not None:
                    raise self._failure
                break

            yield item

    def _signal_stop(self) -> None:
        """Signal consumers that iteration should stop."""
        self._queue.put_nowait(None)

    def _stream_failed(self, error: Exception, *, stream: str) -> None:
        if self._failure is not None:
            return
        failure = RuntimeError(f"DataBentoFeed experimental {stream} failed")
        failure.__cause__ = error
        self._failure = failure
        self._running = False
        self._signal_stop()

    @property
    def stats(self) -> dict[str, Any]:
        """Get feed statistics."""
        return {
            "running": self._running,
            "mode": self.mode,
            "record_count": self._record_count,
            "symbols": self.symbols,
            "replay_speed": self.replay_speed,
            "experimental": True,
            "missing_beta_guarantees": list(DATABENTO_MISSING_GUARANTEES),
        }
