"""Exercise the installed package through a credential-free safe workflow."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from ml4t.backtest.types import Order, OrderSide, OrderStatus, OrderType, Position

from ml4t.live import LiveRiskConfig, SafeBroker, __version__


class SmokeBroker:
    """Minimal venue used to prove that shadow orders cannot reach an adapter."""

    execution_capabilities: frozenset[object] = frozenset()

    def __init__(self) -> None:
        self._connected = False
        self.live_submissions = 0

    @property
    def positions(self) -> dict[str, Position]:
        return {}

    @property
    def pending_orders(self) -> list[Order]:
        return []

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def is_connected_async(self) -> bool:
        return self._connected

    async def get_positions_async(self) -> dict[str, Position]:
        return {}

    async def get_pending_orders_async(self, asset: str | None = None) -> list[Order]:
        return []

    async def get_position_async(self, asset: str) -> Position | None:
        return None

    async def get_account_value_async(self) -> float:
        return 100_000.0

    async def get_cash_async(self) -> float:
        return 100_000.0

    async def submit_order_async(
        self,
        asset: str,
        quantity: float,
        side: OrderSide | None = None,
        order_type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
        stop_price: float | None = None,
        **kwargs: object,
    ) -> Order:
        self.live_submissions += 1
        raise AssertionError("shadow workflow reached the live adapter")

    async def cancel_order_async(self, order_id: str) -> bool:
        return False

    async def replace_order_async(
        self,
        order_id: str,
        *,
        quantity: float | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> Order:
        raise AssertionError("shadow workflow reached order replacement")

    async def close_position_async(self, asset: str) -> Order | None:
        return None


async def exercise() -> None:
    with tempfile.TemporaryDirectory(prefix="ml4t-live-installed-smoke-") as temporary:
        root = Path(temporary)
        venue = SmokeBroker()
        safe = SafeBroker(
            venue,
            LiveRiskConfig(
                shadow_mode=True,
                state_file=str(root / "state.json"),
                journal_file=str(root / "journal.jsonl"),
            ),
        )
        await safe.connect()
        safe.record_market_snapshot("AAPL", 100.0)
        order = await safe.submit_order_async("AAPL", 2, OrderSide.BUY)
        position = await safe.get_position_async("AAPL")
        assert order.status is OrderStatus.FILLED
        assert position is not None and position.quantity == 2
        assert venue.live_submissions == 0
        assert (root / "state.json").is_file()
        await safe.disconnect()
        assert not await venue.is_connected_async()
        assert __version__.startswith("0.1.0b4")


def main() -> None:
    asyncio.run(exercise())


if __name__ == "__main__":
    main()
