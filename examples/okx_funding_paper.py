# ruff: noqa: E402
"""Poll OKX 1-minute candles and funding rates without credentials.

Purpose:
    Demonstrate a public-data workflow for perpetual swaps with no broker and
    no API keys.

Prerequisites:
    - HTTPS access to https://www.okx.com
    - `ml4t-live` installed or this repo checked out locally

Expected Output:
    - A heartbeat every 5 seconds
    - Funding-rate snapshots for BTC, ETH, and SOL perpetuals
    - Simple long/short/flat bias labels based on funding extremes

Runtime:
    About 70 seconds. The script exits on its own.
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

from ml4t.live import OKXFundingFeed

SYMBOLS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
DURATION_SECONDS = 70


def funding_signal(rate: float) -> str:
    if rate >= 0.0001:
        return "short-bias"
    if rate <= -0.0001:
        return "long-bias"
    return "flat"


async def main() -> int:
    feed = OKXFundingFeed(
        symbols=SYMBOLS,
        timeframe="1m",
        poll_interval_seconds=5.0,
    )
    started_at = time.monotonic()
    next_heartbeat = 5.0

    print("Starting OKX funding demo with 1-minute bars.")
    await feed.start()

    try:
        while True:
            elapsed = time.monotonic() - started_at
            if elapsed >= DURATION_SECONDS:
                break

            timeout = max(0.1, min(5.0, DURATION_SECONDS - elapsed))
            try:
                timestamp, data, context = await asyncio.wait_for(feed.__anext__(), timeout=timeout)
            except TimeoutError:
                heartbeat_elapsed = int(time.monotonic() - started_at)
                if heartbeat_elapsed >= next_heartbeat:
                    print(
                        f"[{heartbeat_elapsed:>3}s] waiting for the next complete 1-minute candle"
                    )
                    next_heartbeat += 5.0
                continue

            for symbol in SYMBOLS:
                bar = data.get(symbol)
                funding = context.get(symbol)
                if bar is None or funding is None:
                    continue
                rate = funding["funding_rate"]
                next_time = funding.get("next_funding_time")
                print(
                    f"{timestamp.isoformat()} {symbol} close={bar['close']:.2f}"
                    f" funding={rate:+.5%} signal={funding_signal(rate)}"
                    f" next_funding={next_time.isoformat() if next_time else 'n/a'}"
                )
    finally:
        feed.stop()
        await feed.close()

    print("Finished OKX funding demo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
