# ruff: noqa: E402
"""Show stale-data rejection and daily-loss kill-switch activation.

Purpose:
    Demonstrate two operational risk guards without an external broker:
    stale market data blocks new orders, and a daily-loss breach activates the
    persisted kill switch.

Prerequisites:
    - `ml4t-live` installed or this repo checked out locally

Expected Output:
    - One accepted order with fresh data
    - One stale-data rejection after the market snapshot ages out
    - One daily-loss rejection with kill_switch=True

Runtime:
    About 4 seconds. The script exits on its own.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ml4t.backtest.types import Order, OrderSide, OrderStatus, OrderType, Position

from ml4t.live import CanonicalOrderRequest, LiveRiskConfig, RiskLimitError, SafeBroker


class DemoBroker:
    def __init__(self) -> None:
        self._connected = False
        self.positions: dict[str, Position] = {}
        self.pending_orders: list[Order] = []
        self.account_value = 100_000.0

    def assert_paper_trading(self) -> None:
        """Identify this deterministic demo adapter as a paper venue."""

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def is_connected_async(self) -> bool:
        return self._connected

    def get_position(self, asset: str) -> Position | None:
        return self.positions.get(asset)

    async def get_positions_async(self) -> dict[str, Position]:
        return dict(self.positions)

    async def get_pending_orders_async(self) -> list[Order]:
        return list(self.pending_orders)

    async def get_position_async(self, asset: str) -> Position | None:
        return self.positions.get(asset)

    async def get_account_value_async(self) -> float:
        return self.account_value

    async def get_cash_async(self) -> float:
        return self.account_value

    async def submit_order_async(
        self,
        asset: str,
        quantity: float,
        side: OrderSide | None = None,
        order_type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
        stop_price: float | None = None,
        **kwargs: Any,
    ) -> Order:
        request = CanonicalOrderRequest.from_input(
            asset, quantity, side, order_type, limit_price, stop_price
        )
        order = Order(
            asset=request.asset,
            side=request.side,
            quantity=request.quantity,
            order_type=request.order_type,
            limit_price=request.limit_price,
            stop_price=request.stop_price,
            order_id=f"demo-{len(self.pending_orders) + 1}",
            status=OrderStatus.PENDING,
            created_at=datetime.now(UTC),
        )
        self.pending_orders.append(order)
        return order

    async def cancel_order_async(self, order_id: str) -> bool:
        return False

    async def close_position_async(self, asset: str) -> Order | None:
        return None


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ml4t-live-risk-guard-") as directory:
        state_file = Path(directory) / "state.json"
        raw_broker = DemoBroker()
        safe_broker = SafeBroker(
            raw_broker,
            LiveRiskConfig(
                execution_mode="paper",
                max_order_value=5_000.0,
                max_daily_loss=500.0,
                max_data_staleness_seconds=1.0,
                state_file=str(state_file),
            ),
        )
        await safe_broker.connect()

        safe_broker.record_market_snapshot("DEMO", 100.0)
        order = await safe_broker.submit_order_async("DEMO", 10)
        print(f"fresh_data_order: accepted order_type={order.order_type.value}")

        await asyncio.sleep(1.1)
        try:
            await safe_broker.submit_order_async("DEMO", 1)
        except RiskLimitError as exc:
            print(f"stale_data_block: {exc}")

        safe_broker.record_market_snapshot("DEMO", 101.0)
        raw_broker.account_value = 99_000.0
        try:
            await safe_broker.submit_order_async("DEMO", 1)
        except RiskLimitError as exc:
            print(f"daily_loss_block: {exc}")
            print(f"kill_switch_active: {safe_broker.config.kill_switch_enabled}")

        await safe_broker.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
