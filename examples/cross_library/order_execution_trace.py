"""Exercise the same price-only order through Backtest and a local Live paper adapter."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

import polars as pl
from ml4t.backtest import BacktestConfig, DataFeed, Engine, Strategy
from ml4t.backtest.config import CommissionType, SlippageType
from ml4t.backtest.types import Order, OrderSide, OrderStatus, OrderType, Position
from ml4t.specs import (
    AssetTarget,
    CanonicalTargetIntent,
    ExecutionBehavior,
    ExecutionFill,
    IntentReason,
    LifecyclePhase,
    ResidualPolicy,
    RoundingPolicy,
    TargetMeasure,
)
from ml4t.specs import OrderSide as SpecOrderSide

from ml4t.live import LiveEngine, LiveRiskConfig, SafeBroker, default_live_execution_policy
from ml4t.live.safety import RiskLimitError

INITIAL_CASH = Decimal("100000")
FEE_PER_SHARE = Decimal("0.01")
AUCTION_TIME = datetime(2026, 8, 11, 13, 30, tzinfo=UTC)


class OpeningRangeBreakout(Strategy):
    def __init__(self):
        self.range_high = None

    def on_data(self, timestamp, data, context, broker):
        if timestamp.date() != date(2026, 8, 10):
            return
        bar = data["SPY"]
        if self.range_high is None:
            self.range_high = bar["high"]
            return
        if bar["close"] <= self.range_high:
            return
        timestamp = timestamp.replace(tzinfo=UTC) if timestamp.tzinfo is None else timestamp
        broker.register_target_intent(
            CanonicalTargetIntent(
                intent_id="opening-range-spy",
                decision_time=timestamp,
                information_cutoff=timestamp,
                effective_session=date(2026, 8, 11),
                effective_phase=LifecyclePhase.PRE_OPEN,
                targets=(AssetTarget("SPY", TargetMeasure.QUANTITY, 100),),
                idempotency_key="opening-range-spy-key",
                measure=TargetMeasure.QUANTITY,
                cash_buffer=0,
                rounding=RoundingPolicy.TOWARD_ZERO,
                residual=ResidualPolicy.KEEP_CASH,
                reason=IntentReason.REBALANCE,
            )
        )


def bar(day, minute, open_price, close):
    timestamp = datetime(2026, 8, day, 13, minute, tzinfo=UTC)
    return (
        timestamp,
        {
            "SPY": {
                "open": open_price,
                "high": max(open_price, close),
                "low": min(open_price, close),
                "close": close,
                "volume": 1_000_000.0,
            }
        },
        {},
    )


EVENTS = [
    bar(10, 31, 99.00, 99.50),
    bar(10, 32, 99.50, 100.00),
    bar(11, 30, 100.01, 100.01),
    bar(11, 31, 100.01, 100.01),
    bar(11, 32, 100.05, 100.05),
    bar(11, 33, 100.10, 100.10),
    bar(11, 34, 100.10, 100.10),
]


def market_data_digest(events):
    records = [(timestamp.isoformat(), bars, context) for timestamp, bars, context in events]
    return sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()


class ReplayBroker:
    def __init__(self, executions, execution_prices=None):
        self._connected = False
        self._pending_orders = []
        self._positions = {}
        self.submit_calls = 0
        self.executions = executions
        self.execution_prices = execution_prices or {}
        self.fills = []
        self.cash = INITIAL_CASH
        self.notional = Decimal(0)
        self.mark = Decimal(0)
        self.orders = {}
        self.replacements = {}

    async def get_cash_async(self):
        return float(self.cash)

    async def get_account_value_async(self):
        position = self.positions.get("SPY")
        return float(self.cash + (int(position.quantity) * self.mark if position else 0))

    execution_capabilities = frozenset()

    @property
    def is_connected(self):
        return self._connected

    @property
    def positions(self):
        return dict(self._positions)

    @property
    def pending_orders(self):
        return list(self._pending_orders)

    async def connect(self):
        self._connected = True

    async def disconnect(self):
        self._connected = False

    async def is_connected_async(self):
        return self.is_connected

    def assert_paper_trading(self):
        """Identify this local fixture to SafeBroker."""

    async def get_positions_async(self):
        return self.positions

    async def get_position_async(self, asset):
        return self._positions.get(asset)

    async def get_pending_orders_async(self, asset=None):
        return [order for order in self.pending_orders if asset is None or order.asset == asset]

    async def submit_order_async(
        self,
        asset,
        quantity,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        limit_price=None,
        stop_price=None,
        **kwargs,
    ):
        self.submit_calls += 1
        order = Order(
            asset=asset,
            quantity=quantity,
            side=side,
            order_type=order_type,
            limit_price=limit_price,
            stop_price=stop_price,
            order_id=f"paper-{self.submit_calls}",
            created_at=self.now,
            target_intent_id=kwargs.get("target_intent_id"),
            child_intent_id=kwargs.get("child_intent_id"),
        )
        self._pending_orders.append(order)
        self.orders[order.order_id] = order
        return order

    async def cancel_order_async(self, order_id):
        for order in self.pending_orders:
            if order.order_id == order_id:
                order.status = OrderStatus.CANCELLED
                order.cancelled_at = self.now
                self._pending_orders.remove(order)
                return True
        return False

    def advance(self, timestamp, prices):
        self.now = timestamp
        self.mark = Decimal(str(prices["SPY"]["close"]))
        if timestamp.day != 11 or timestamp.minute not in self.executions:
            return
        execution = self.executions[timestamp.minute]
        if isinstance(execution, tuple):
            order_id, execution = execution
            order = self.orders[order_id]
        else:
            (order,) = self.pending_orders
        if execution is None:
            order.status = OrderStatus.CANCELLED
            order.cancelled_at = timestamp
            self._pending_orders.remove(order)
            return
        quantity = execution
        price = Decimal(self.execution_prices.get(timestamp.minute, "100.01"))
        fee = quantity * FEE_PER_SHARE
        self.notional += quantity * price
        self.cash -= quantity * price + fee
        order.filled_quantity = (order.filled_quantity or 0) + quantity
        order.filled_price = float(price)
        execution_time = (
            AUCTION_TIME if order.order_id == "paper-1" and timestamp.minute == 31 else timestamp
        )
        order.filled_at = execution_time
        if order.filled_quantity == order.quantity and order in self._pending_orders:
            order.status = OrderStatus.FILLED
            self._pending_orders.remove(order)
        previous = self._positions.get(order.asset)
        total = quantity + (int(previous.quantity) if previous else 0)
        self._positions[order.asset] = Position(
            asset=order.asset,
            quantity=total,
            entry_price=float(self.notional / total),
            entry_time=AUCTION_TIME,
            current_price=float(self.mark),
        )
        self.fills.append(
            ExecutionFill(
                execution_id=f"paper-execution-{len(self.fills) + 1}",
                order_id=order.order_id,
                asset=order.asset,
                side=SpecOrderSide(order.side.value),
                quantity=quantity,
                price=float(price),
                fee=float(fee),
                fee_currency="USD",
                executed_at=execution_time,
                received_at=timestamp,
                venue="local-replay",
                multiplier=1.0,
                currency="USD",
            )
        )


class ReplayFeed:
    def __init__(self, broker, safe, replace_original=False):
        self.broker = broker
        self.safe = safe
        self.replace_original = replace_original
        self.running = False
        self.events = iter(EVENTS)

    async def start(self):
        self.running = True

    def stop(self):
        self.running = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        event = next(self.events, None) if self.running else None
        if event is None:
            raise StopAsyncIteration
        timestamp, prices, context = event
        self.broker.advance(timestamp, prices)
        if self.replace_original and timestamp.day == 11 and timestamp.minute == 31:
            original = self.broker.pending_orders[0]
            replacement = await self.safe.replace_order_async(original.order_id, quantity=30)
            self.broker.replacements[replacement.order_id] = original.order_id
        return (
            timestamp,
            {asset: dict(bar, price=bar["close"], signals={}) for asset, bar in prices.items()},
            context,
        )


def policy():
    return default_live_execution_policy(opening_auction=ExecutionBehavior.CLIENT)


def backtest(events=EVENTS):
    feed = DataFeed(
        prices_df=pl.DataFrame(
            [
                {"timestamp": timestamp, "asset": asset, **bar}
                for timestamp, data, _ in events
                for asset, bar in data.items()
            ]
        )
    )
    trace = (
        Engine(
            feed,
            OpeningRangeBreakout(),
            BacktestConfig(
                initial_cash=float(INITIAL_CASH),
                commission_type=CommissionType.PER_SHARE,
                commission_per_share=float(FEE_PER_SHARE),
                slippage_type=SlippageType.NONE,
                retain_intent_history=True,
            ),
            execution_policy=policy(),
            holdings_sampling="bar",
        )
        .run()
        .to_order_trace(currency="USD")
    )
    return {**trace, "market_data_sha256": market_data_digest(events)}


async def paper_replay(
    state_dir,
    executions,
    *,
    max_order_shares=10_000,
    replace_original=False,
    restart=False,
    execution_prices=None,
):
    broker = ReplayBroker(executions, execution_prices)
    config = LiveRiskConfig(
        execution_mode="paper",
        max_position_value=200_000,
        max_order_value=200_000,
        max_position_shares=10_000,
        max_order_shares=max_order_shares,
        dedup_window_seconds=0,
        state_file=str(state_dir / "runtime-state.json"),
    )
    safe = SafeBroker(broker, config)
    engine = LiveEngine(
        OpeningRangeBreakout(),
        safe,
        ReplayFeed(broker, safe, replace_original),
        execution_policy=policy(),
    )
    await engine.connect()
    try:
        await engine.run()
    except RiskLimitError:
        if max_order_shares >= 100:
            raise
    assert broker.submit_calls == (1 + int(replace_original) if max_order_shares >= 100 else 0)
    reports = broker.fills
    if restart:
        restarted = SafeBroker(broker, config)
        engine = LiveEngine(
            OpeningRangeBreakout(),
            restarted,
            ReplayFeed(broker, restarted),
            execution_policy=policy(),
        )
        reports = [*reports, replace(reports[0], received_at=broker.now)]
    trace = engine.to_order_trace(
        fills=reports,
        replacements=broker.replacements,
        account={
            "timestamp": broker.now.isoformat(),
            "cash": await broker.get_cash_async(),
            "equity": await broker.get_account_value_async(),
            "currency": "USD",
            "positions": {asset: position.quantity for asset, position in broker.positions.items()},
        },
    )
    return {**trace, "market_data_sha256": market_data_digest(EVENTS)}


async def main():
    from ml4t.diagnostic.integration import ArrivalQuote, compare_order_traces

    historical = backtest()
    with TemporaryDirectory() as directory:
        control = await paper_replay(Path(directory) / "control", {31: 100})
        partial = await paper_replay(Path(directory) / "partial", {31: 60, 32: 20, 33: None})
        restarted = await paper_replay(
            Path(directory) / "restart", {31: 60, 32: 20, 33: None}, restart=True
        )
        replacement = await paper_replay(
            Path(directory) / "replacement",
            {31: 60, 32: ("paper-1", 10), 33: 30},
            replace_original=True,
        )
        rejected = await paper_replay(Path(directory) / "rejected", {}, max_order_shares=50)
        pending = await paper_replay(Path(directory) / "pending", {31: 60, 32: 20})
        adverse_price = await paper_replay(
            Path(directory) / "adverse-price",
            {31: 60, 32: 20, 33: None},
            execution_prices={32: "100.05"},
        )
    reports = {
        name: compare_order_traces(
            historical,
            trace,
            child_intent_id="opening-range-spy:SPY",
            currency="USD",
            decision_price="100.00",
            terminal_price="100.10",
        )
        for name, trace in (
            ("control", control),
            ("partial", partial),
            ("restarted", restarted),
            ("replacement", replacement),
            ("rejected", rejected),
        )
    }
    assert reports["control"]["first_differing_stage_compared"] is None
    assert reports["partial"]["first_differing_stage_compared"] == "execution_outcome"
    assert Decimal(reports["partial"]["live_minus_backtest_shortfall"]) == Decimal("1.60")
    assert Decimal(str(historical["account"]["equity"])) - Decimal(
        str(partial["account"]["equity"])
    ) == Decimal("1.60")
    assert (
        reports["restarted"]["live_minus_backtest_shortfall"]
        == reports["partial"]["live_minus_backtest_shortfall"]
    )
    assert len(reports["restarted"]["live"]["fills"]) == 2
    assert reports["replacement"]["live"]["observed_filled_quantity"] == "100.0"
    assert reports["replacement"]["live"]["unresolved"] == []
    assert reports["replacement"]["first_differing_stage_compared"] == "replacement"
    assert reports["rejected"]["first_differing_stage_compared"] == "risk_rejection"
    missing_id = {
        **partial,
        "fills": [{**partial["fills"][0], "execution_id": None}, partial["fills"][1]],
    }
    unresolved = compare_order_traces(
        historical, missing_id, child_intent_id="opening-range-spy:SPY", currency="USD"
    )
    assert unresolved["first_differing_stage_compared"] == "unresolved"
    assert unresolved["live"]["total_shortfall"] is None
    no_quote = compare_order_traces(
        historical, partial, child_intent_id="opening-range-spy:SPY", currency="USD"
    )
    assert no_quote["live"]["fees"] == "0.8"
    assert no_quote["live"]["execution_cost"] is None
    status_only = compare_order_traces(
        pending,
        partial,
        child_intent_id="opening-range-spy:SPY",
        currency="USD",
        decision_price="100.00",
        terminal_price="100.10",
    )
    assert status_only["first_differing_stage_compared"] == "order_status"
    assert Decimal(status_only["live_minus_backtest_shortfall"]) == Decimal(0)
    reports["status_only"] = status_only

    quote = ArrivalQuote(
        asset="SPY", currency="USD", observed_at=AUCTION_TIME, bid="100.00", ask="100.01"
    )
    quoted = compare_order_traces(
        historical,
        adverse_price,
        child_intent_id="opening-range-spy:SPY",
        currency="USD",
        decision_price="100.00",
        terminal_price="100.10",
        backtest_arrival_quote=quote,
        live_arrival_quote=quote,
    )
    assert Decimal(quoted["live"]["delay_cost"]) == Decimal("0.40")
    assert Decimal(quoted["live"]["spread_cost"]) == Decimal("0.40")
    assert Decimal(quoted["live"]["price_vs_touch_cost"]) == Decimal("0.80")
    assert Decimal(quoted["live"]["execution_cost"]) == Decimal("2.40")
    assert Decimal(quoted["live_minus_backtest_shortfall"]) == Decimal("2.40")
    assert Decimal(str(historical["account"]["equity"])) - Decimal(
        str(adverse_price["account"]["equity"])
    ) == Decimal("2.40")
    assert reports["partial"]["live"]["price_vs_touch_cost"] is None
    late_quote = compare_order_traces(
        historical,
        adverse_price,
        child_intent_id="opening-range-spy:SPY",
        currency="USD",
        decision_price="100.00",
        terminal_price="100.10",
        live_arrival_quote=replace(quote, observed_at=EVENTS[-1][0]),
    )
    assert late_quote["live"]["delay_cost"] is None
    assert late_quote["live"]["execution_cost"] == quoted["live"]["execution_cost"]
    reports["quoted_adverse_price"] = quoted

    different_market = backtest([*EVENTS[:-1], bar(11, 34, 100.11, 100.11)])
    data_difference = compare_order_traces(
        different_market,
        control,
        child_intent_id="opening-range-spy:SPY",
        currency="USD",
        decision_price="100.00",
        terminal_price="100.10",
    )
    assert data_difference["first_differing_stage_compared"] == "market_data"
    assert data_difference["live_minus_backtest_shortfall"] is None
    reports["different_market_data"] = data_difference
    print(json.dumps(reports, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
