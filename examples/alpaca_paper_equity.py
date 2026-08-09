# ruff: noqa: E402
"""Run a short Alpaca paper-trading equity demo with moving averages.

Purpose:
    Show how to connect `LiveEngine`, `SafeBroker`, `AlpacaBroker`, and
    `AlpacaDataFeed` in paper mode with a tiny moving-average crossover
    strategy for SPY, QQQ, and IWM.

Prerequisites:
    - `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` set in the environment
    - Alpaca paper account enabled
    - `ml4t-live` installed or this repo checked out locally

Expected Output:
    - A heartbeat every 5 seconds
    - Minute-bar updates when market data is available
    - Small paper-order attempts when the toy crossover flips

Runtime:
    About 95 seconds. The script exits on its own.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if SRC.exists():
    sys.path.insert(0, str(SRC))

from ml4t.backtest import Strategy
from ml4t.backtest.types import OrderSide

from ml4t.live import AlpacaBroker, AlpacaDataFeed, LiveEngine, LiveRiskConfig, SafeBroker

SYMBOLS = ["SPY", "QQQ", "IWM"]
DURATION_SECONDS = 95


class MovingAverageCrossoverStrategy(Strategy):
    def __init__(self, fast_period: int = 2, slow_period: int = 4) -> None:
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.prices: dict[str, list[float]] = {symbol: [] for symbol in SYMBOLS}
        self.last_signal: dict[str, str | None] = dict.fromkeys(SYMBOLS)

    def on_data(self, timestamp, data, context, broker) -> None:
        for symbol in SYMBOLS:
            bar = data.get(symbol)
            if bar is None:
                continue

            price_history = self.prices[symbol]
            price_history.append(float(bar["close"]))
            if len(price_history) > self.slow_period:
                del price_history[: -self.slow_period]

            if len(price_history) < self.slow_period:
                print(
                    f"{timestamp.isoformat()} {symbol} building history"
                    f" {len(price_history)}/{self.slow_period} close={bar['close']:.2f}"
                )
                continue

            fast_ma = sum(price_history[-self.fast_period :]) / self.fast_period
            slow_ma = sum(price_history) / len(price_history)
            signal = "buy" if fast_ma > slow_ma else "sell"
            if signal == self.last_signal[symbol]:
                continue
            self.last_signal[symbol] = signal

            position = broker.get_position(symbol)
            quantity = position.quantity if position is not None else 0
            print(
                f"{timestamp.isoformat()} {symbol} close={bar['close']:.2f}"
                f" fast={fast_ma:.2f} slow={slow_ma:.2f} signal={signal}"
            )

            if signal == "buy" and quantity <= 0:
                broker.submit_order(symbol, 1, side=OrderSide.BUY)
            elif signal == "sell" and quantity > 0:
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
        print(f"[{elapsed:>3}s] alpaca paper demo alive positions={position_text}")


async def stop_after(duration: int, engine: LiveEngine) -> None:
    await asyncio.sleep(duration)
    await engine.stop()


async def main() -> int:
    api_key = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        print("Set ALPACA_API_KEY and ALPACA_SECRET_KEY before running this paper demo.")
        return 1

    broker = AlpacaBroker(api_key=api_key, secret_key=secret_key, paper=True)
    feed = AlpacaDataFeed(
        api_key=api_key,
        secret_key=secret_key,
        symbols=SYMBOLS,
        data_type="bars",
        feed="iex",
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
    engine = LiveEngine(MovingAverageCrossoverStrategy(), safe_broker, feed)

    print("Connecting to Alpaca paper trading.")
    try:
        await engine.connect()
    except Exception as exc:
        print(f"Could not connect to Alpaca paper trading: {exc}")
        return 1

    print(f"Running Alpaca paper demo for {DURATION_SECONDS}s.")
    heartbeat_task = asyncio.create_task(heartbeat(safe_broker))
    stop_task = asyncio.create_task(stop_after(DURATION_SECONDS, engine))

    try:
        await engine.run()
    finally:
        heartbeat_task.cancel()
        stop_task.cancel()
        await asyncio.gather(heartbeat_task, stop_task, return_exceptions=True)
        await engine.stop()

    print("Finished Alpaca paper demo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
