# ruff: noqa: E402
"""Run a short Interactive Brokers paper-trading demo against TWS or Gateway.

Purpose:
    Demonstrate a tiny momentum strategy with `IBBroker` and `IBDataFeed`
    against a paper account on port 7497.

Prerequisites:
    - TWS or IB Gateway running locally on 127.0.0.1:7497
    - Paper account with API access enabled
    - Market-data subscriptions for the selected symbols
    - `ml4t-live` installed or this repo checked out locally

Expected Output:
    - A heartbeat every 5 seconds
    - Tick updates for a few large-cap names when market data is available
    - Small paper-order attempts when the toy signal flips

Runtime:
    About 75 seconds. The script exits on its own.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if SRC.exists():
    sys.path.insert(0, str(SRC))

from ml4t.backtest import Strategy
from ml4t.backtest.types import OrderSide

from ml4t.live import IBBroker, IBDataFeed, LiveEngine, LiveRiskConfig, SafeBroker

SYMBOLS = ["AAPL", "MSFT", "NVDA"]
DURATION_SECONDS = 75


class ToyMomentumStrategy(Strategy):
    def __init__(self) -> None:
        self.last_price: dict[str, float] = {}

    def on_data(self, timestamp, data, context, broker) -> None:
        for symbol in SYMBOLS:
            tick = data.get(symbol)
            if tick is None:
                continue

            price = float(tick["price"])
            previous = self.last_price.get(symbol)
            self.last_price[symbol] = price
            if previous is None:
                print(f"{timestamp.isoformat()} {symbol} first tick={price:.2f}")
                continue

            move = (price - previous) / previous if previous else 0.0
            position = broker.get_position(symbol)
            quantity = position.quantity if position is not None else 0
            if move >= 0.002 and quantity <= 0:
                print(f"{timestamp.isoformat()} {symbol} momentum up {move:.3%} -> buy 1")
                broker.submit_order(symbol, 1, side=OrderSide.BUY)
            elif move <= -0.002 and quantity > 0:
                print(
                    f"{timestamp.isoformat()} {symbol} momentum down {move:.3%} -> sell {int(quantity)}"
                )
                broker.submit_order(symbol, int(quantity), side=OrderSide.SELL)


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
        print(f"[{elapsed:>3}s] ib paper demo alive positions={position_text}")


async def stop_after(duration: int, engine: LiveEngine) -> None:
    await asyncio.sleep(duration)
    await engine.stop()


async def can_reach_tws(host: str, port: int) -> bool:
    try:
        reader, writer = await asyncio.open_connection(host, port)
    except OSError:
        return False
    writer.close()
    await writer.wait_closed()
    return True


async def main() -> int:
    host = "127.0.0.1"
    port = 7497
    if not await can_reach_tws(host, port):
        print(
            "Could not reach TWS or IB Gateway on 127.0.0.1:7497. "
            "Start the paper session and enable API access before rerunning."
        )
        return 1

    broker = IBBroker(host=host, port=port, client_id=77)
    feed = IBDataFeed(
        broker.ib,
        symbols=SYMBOLS,
        tick_throttle_ms=1_000,
        experimental=True,
    )
    safe_broker = SafeBroker(
        broker,
        LiveRiskConfig(
            execution_mode="paper",
            max_position_value=5_000.0,
            max_order_value=1_000.0,
            max_positions=len(SYMBOLS),
        ),
    )
    engine = LiveEngine(ToyMomentumStrategy(), safe_broker, feed)

    print("Connecting to Interactive Brokers paper trading.")
    try:
        await engine.connect()
    except Exception as exc:
        print(f"Could not connect to IB paper trading on 127.0.0.1:7497. Details: {exc}")
        return 1

    print(f"Running IB paper demo for {DURATION_SECONDS}s.")
    heartbeat_task = asyncio.create_task(heartbeat(safe_broker))
    stop_task = asyncio.create_task(stop_after(DURATION_SECONDS, engine))

    try:
        await engine.run()
    finally:
        heartbeat_task.cancel()
        stop_task.cancel()
        await asyncio.gather(heartbeat_task, stop_task, return_exceptions=True)
        await engine.stop()

    print("Finished IB paper demo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
