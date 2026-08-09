# ruff: noqa: E402
"""Show SafeBroker startup reconciliation against a persisted state snapshot.

Purpose:
    Demonstrate the operational startup check added by `SafeBroker.connect()`.
    The demo writes a persisted snapshot that disagrees with the broker's live
    positions and pending orders, then prints the reconciliation report.

Prerequisites:
    - `ml4t-live` installed or this repo checked out locally

Expected Output:
    - A JSON reconciliation report with missing and unexpected positions/orders

Runtime:
    Under 2 seconds. The script exits on its own.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ml4t.backtest.types import Order, OrderSide, OrderStatus, OrderType, Position
from ml4t.specs import ExecutionCapability

from ml4t.live import LiveRiskConfig, RiskState, SafeBroker


class DemoBroker:
    def __init__(self) -> None:
        self._connected = False
        self.positions = {
            "MSFT": Position(
                asset="MSFT",
                quantity=5,
                entry_price=410.0,
                entry_time=datetime.now(UTC),
            )
        }
        self.pending_orders = [
            Order(
                asset="NVDA",
                side=OrderSide.BUY,
                quantity=2,
                order_type=OrderType.LIMIT,
                limit_price=900.0,
                order_id="venue-pending-1",
                status=OrderStatus.PENDING,
                created_at=datetime.now(UTC),
            )
        ]

    def assert_paper_trading(self) -> None:
        """Identify this deterministic demo adapter as a paper venue."""

    @property
    def execution_capabilities(self) -> frozenset[ExecutionCapability]:
        """Declare that this demo needs no optional execution behavior."""
        return frozenset()

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def is_connected_async(self) -> bool:
        return self._connected

    async def get_positions_async(self) -> dict[str, Position]:
        return dict(self.positions)

    async def get_pending_orders_async(self) -> list[Order]:
        return list(self.pending_orders)

    async def get_position_async(self, asset: str) -> Position | None:
        return self.positions.get(asset)

    async def get_account_value_async(self) -> float:
        return 100_000.0

    async def get_cash_async(self) -> float:
        return 75_000.0

    async def submit_order_async(self, *args: Any, **kwargs: Any) -> Order:
        raise RuntimeError("This demo does not place orders")

    async def cancel_order_async(self, order_id: str) -> bool:
        return False

    async def close_position_async(self, asset: str) -> Order | None:
        return None


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ml4t-live-reconciliation-") as directory:
        state_file = Path(directory) / "state.json"
        persisted_state = RiskState(
            date=datetime.now(UTC).date().isoformat(),
            persisted_positions={"AAPL": 10.0},
            persisted_pending_orders=[
                {
                    "asset": "AAPL",
                    "side": "buy",
                    "quantity": 10.0,
                    "order_type": "limit",
                    "limit_price": 150.0,
                }
            ],
        )
        RiskState.save_atomic(persisted_state, str(state_file))

        safe_broker = SafeBroker(
            DemoBroker(),
            LiveRiskConfig(execution_mode="paper", state_file=str(state_file)),
        )
        await safe_broker.connect()

        print("Startup reconciliation report:")
        print(json.dumps(safe_broker.reconciliation_report, indent=2, sort_keys=True))

        await safe_broker.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
