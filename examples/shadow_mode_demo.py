# ruff: noqa: E402
"""Show SafeBroker shadow mode and VirtualPortfolio without external services.

Purpose:
    Demonstrate the full `LiveEngine` and `SafeBroker` workflow with
    `execution_mode="shadow"` and a synthetic data feed, so readers can see order
    intents and virtual positions without credentials or a broker connection.

Prerequisites:
    - `ml4t-live` installed or this repo checked out locally

Expected Output:
    - A heartbeat every 5 seconds
    - Synthetic price bars every second
    - Shadow-mode order intents and virtual position updates

Runtime:
    About 65 seconds. The script exits on its own.
"""

from __future__ import annotations

import asyncio
import math
import os
import tempfile
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ml4t.backtest import Strategy
from ml4t.backtest.types import OrderSide, Position
from ml4t.specs import ExecutionCapability

from ml4t.live import LiveEngine, LiveRiskConfig, SafeBroker

SYMBOL = "DEMO"
DURATION_SECONDS = int(os.environ.get("ML4T_EXAMPLE_DURATION_SECONDS", "65"))
TICK_SECONDS = float(os.environ.get("ML4T_EXAMPLE_TICK_SECONDS", "1"))


class DemoBroker:
    def __init__(self) -> None:
        self._connected = False
        self._positions: dict[str, Position] = {}
        self._pending_orders = []
        self._cash = 100_000.0

    @property
    def positions(self) -> dict[str, Position]:
        return dict(self._positions)

    @property
    def pending_orders(self) -> list:
        return list(self._pending_orders)

    @property
    def execution_capabilities(self) -> frozenset[ExecutionCapability]:
        """Declare that this demo uses only baseline market orders."""
        return frozenset()

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def is_connected_async(self) -> bool:
        return self._connected

    async def get_positions_async(self) -> dict[str, Position]:
        return dict(self._positions)

    async def get_pending_orders_async(self) -> list:
        return list(self._pending_orders)

    async def get_position_async(self, asset: str) -> Position | None:
        return self._positions.get(asset)

    async def get_account_value_async(self) -> float:
        return self._cash

    async def get_cash_async(self) -> float:
        return self._cash

    async def submit_order_async(self, *args, **kwargs):
        raise RuntimeError("DemoBroker should never receive orders in shadow mode")

    async def cancel_order_async(self, order_id: str) -> bool:
        return False

    async def close_position_async(self, asset: str):
        return None


class SyntheticBarFeed:
    def __init__(self, duration_seconds: int) -> None:
        self.duration_seconds = duration_seconds
        self._running = False
        self._index = 0
        self._started_at = datetime.now(UTC).replace(microsecond=0)

    async def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    def __aiter__(self) -> AsyncIterator[tuple[datetime, dict, dict]]:
        return self

    async def __anext__(self) -> tuple[datetime, dict, dict]:
        if not self._running or self._index >= self.duration_seconds:
            raise StopAsyncIteration

        timestamp = self._started_at + timedelta(seconds=self._index)
        close = 100.0 + math.sin(self._index / 4.0) * 1.8 + (self._index * 0.03)
        open_price = close - 0.25
        high = close + 0.35
        low = close - 0.40
        volume = 1_000 + (self._index * 25)
        self._index += 1
        await asyncio.sleep(TICK_SECONDS)
        return (
            timestamp,
            {
                SYMBOL: {
                    "open": round(open_price, 2),
                    "high": round(high, 2),
                    "low": round(low, 2),
                    "close": round(close, 2),
                    "volume": volume,
                }
            },
            {},
        )


class ShadowMomentumStrategy(Strategy):
    def __init__(self) -> None:
        self.prices: list[float] = []
        self.last_signal: str | None = None

    def on_data(self, timestamp, data, context, broker) -> None:
        bar = data.get(SYMBOL)
        if bar is None:
            return

        close = float(bar["close"])
        self.prices.append(close)
        if len(self.prices) > 6:
            del self.prices[:-6]

        if len(self.prices) < 6:
            print(
                f"{timestamp.isoformat()} close={close:.2f} building_history={len(self.prices)}/6"
            )
            return

        fast_ma = sum(self.prices[-3:]) / 3
        slow_ma = sum(self.prices) / len(self.prices)
        signal = "buy" if fast_ma > slow_ma else "sell"
        position = broker.get_position(SYMBOL)
        quantity = position.quantity if position is not None else 0

        print(
            f"{timestamp.isoformat()} {SYMBOL} close={close:.2f}"
            f" fast={fast_ma:.2f} slow={slow_ma:.2f} position={quantity:g}"
        )

        if signal == self.last_signal:
            return
        self.last_signal = signal

        if signal == "buy" and quantity <= 0:
            broker.submit_order(SYMBOL, 10, side=OrderSide.BUY)
        elif signal == "sell" and quantity > 0:
            broker.submit_order(SYMBOL, int(quantity), side=OrderSide.SELL)


async def heartbeat(safe_broker: SafeBroker) -> None:
    started_at = time.monotonic()
    while True:
        await asyncio.sleep(5)
        elapsed = int(time.monotonic() - started_at)
        positions = safe_broker.positions
        if positions:
            position_text = ", ".join(
                f"{asset}:{position.quantity:g}" for asset, position in sorted(positions.items())
            )
        else:
            position_text = "flat"
        cash = await safe_broker.get_cash_async()
        account_value = await safe_broker.get_account_value_async()
        print(
            f"[{elapsed:>3}s] shadow demo alive positions={position_text}"
            f" cash={cash:,.2f} account_value={account_value:,.2f}"
        )


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ml4t-live-shadow-demo-") as directory:
        state_file = Path(directory) / "state.json"
        safe_broker = SafeBroker(
            DemoBroker(),
            LiveRiskConfig(
                execution_mode="shadow",
                max_position_value=5_000.0,
                max_order_value=2_000.0,
                max_positions=1,
                state_file=str(state_file),
            ),
        )
        engine = LiveEngine(
            ShadowMomentumStrategy(), safe_broker, SyntheticBarFeed(DURATION_SECONDS)
        )

        print("Starting shadow mode demo with a synthetic feed.")
        await engine.connect()
        heartbeat_task = asyncio.create_task(heartbeat(safe_broker))

        try:
            await engine.run()
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            await engine.stop()

        positions = safe_broker.positions
        if positions:
            summary = ", ".join(
                f"{asset}:{position.quantity:g}" for asset, position in sorted(positions.items())
            )
        else:
            summary = "flat"
        print(f"Finished shadow mode demo. final_positions={summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
