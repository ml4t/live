"""Canonical validation for strategy-facing live order requests."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ml4t.backtest.types import Order, OrderSide, OrderStatus, OrderType
from ml4t.specs import ExecutionCapability


class OrderValidationError(ValueError):
    """Raised before side effects when an order request is not canonical."""


class UnsupportedOrderCapabilityError(OrderValidationError):
    """Raised when a venue has not declared the capability required by an order."""


class BrokerOrderContractError(RuntimeError):
    """Raised when an adapter result differs from the request it accepted."""


_REQUIRED_CAPABILITY = {
    OrderType.LIMIT: ExecutionCapability.LIMIT,
    OrderType.STOP: ExecutionCapability.STOP,
    OrderType.STOP_LIMIT: ExecutionCapability.STOP_LIMIT,
    OrderType.TRAILING_STOP: ExecutionCapability.TRAILING_STOP,
    OrderType.MOC: ExecutionCapability.CLOSE_AUCTION,
}


def _positive_optional(value: Any, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise OrderValidationError(f"{name} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise OrderValidationError(f"{name} must be numeric") from error
    if not math.isfinite(number) or number <= 0:
        raise OrderValidationError(f"{name} must be finite and positive")
    return number


def _normalized_capabilities(values: Iterable[Any]) -> frozenset[ExecutionCapability]:
    capabilities: set[ExecutionCapability] = set()
    for value in values:
        try:
            capabilities.add(ExecutionCapability(value))
        except (TypeError, ValueError):
            continue
    return frozenset(capabilities)


@dataclass(frozen=True, slots=True)
class CanonicalOrderRequest:
    """One unsigned venue request used unchanged for checks and submission."""

    asset: str
    quantity: float
    side: OrderSide
    order_type: OrderType
    limit_price: float | None = None
    stop_price: float | None = None

    @classmethod
    def from_input(
        cls,
        asset: str,
        quantity: Any,
        side: OrderSide | None,
        order_type: OrderType,
        limit_price: Any = None,
        stop_price: Any = None,
        *,
        capabilities: Iterable[Any] = (),
    ) -> CanonicalOrderRequest:
        if not isinstance(asset, str) or not asset.strip():
            raise OrderValidationError("asset must be a non-empty string")
        normalized_asset = asset.strip().upper()
        if isinstance(quantity, bool):
            raise OrderValidationError("quantity must be numeric")
        try:
            signed_quantity = float(quantity)
        except (TypeError, ValueError) as error:
            raise OrderValidationError("quantity must be numeric") from error
        if not math.isfinite(signed_quantity) or signed_quantity == 0:
            raise OrderValidationError("quantity must be finite and non-zero")
        if side is None:
            normalized_side = OrderSide.BUY if signed_quantity > 0 else OrderSide.SELL
            unsigned_quantity = abs(signed_quantity)
        else:
            if not isinstance(side, OrderSide):
                raise OrderValidationError("side must be an OrderSide")
            if signed_quantity < 0:
                raise OrderValidationError(
                    "quantity must be positive and unsigned when side is provided"
                )
            normalized_side = side
            unsigned_quantity = signed_quantity
        if not isinstance(order_type, OrderType):
            raise OrderValidationError("order_type must be an OrderType")

        normalized_limit = _positive_optional(limit_price, "limit_price")
        normalized_stop = _positive_optional(stop_price, "stop_price")
        if order_type is OrderType.MARKET:
            if normalized_limit is not None or normalized_stop is not None:
                raise OrderValidationError("market orders do not accept limit or stop prices")
        elif order_type is OrderType.MOC:
            if normalized_limit is not None or normalized_stop is not None:
                raise OrderValidationError("MOC orders do not accept limit or stop prices")
        elif order_type is OrderType.LIMIT:
            if normalized_limit is None or normalized_stop is not None:
                raise OrderValidationError("limit orders require only limit_price")
        elif order_type is OrderType.STOP:
            if normalized_stop is None or normalized_limit is not None:
                raise OrderValidationError("stop orders require only stop_price")
        elif order_type is OrderType.STOP_LIMIT:
            if normalized_limit is None or normalized_stop is None:
                raise OrderValidationError("stop-limit orders require limit_price and stop_price")
        else:
            raise UnsupportedOrderCapabilityError(
                f"order type {order_type.value!r} has no canonical live request"
            )

        required = _REQUIRED_CAPABILITY.get(order_type)
        declared = _normalized_capabilities(capabilities)
        if required is not None and required not in declared:
            raise UnsupportedOrderCapabilityError(
                f"order type {order_type.value!r} requires capability {required.value!r}"
            )
        return cls(
            asset=normalized_asset,
            quantity=unsigned_quantity,
            side=normalized_side,
            order_type=order_type,
            limit_price=normalized_limit,
            stop_price=normalized_stop,
        )

    def validate_result(self, order: Order) -> None:
        """Require the adapter result to describe this exact request."""
        if not isinstance(order, Order):
            raise BrokerOrderContractError("broker submission did not return an Order")
        if (
            order.asset.upper() != self.asset
            or order.side is not self.side
            or order.order_type is not self.order_type
            or not math.isclose(float(order.quantity), self.quantity, rel_tol=0, abs_tol=1e-12)
            or order.limit_price != self.limit_price
            or order.stop_price != self.stop_price
        ):
            raise BrokerOrderContractError("broker result differs from the canonical order request")
        if not order.order_id or not isinstance(order.status, OrderStatus):
            raise BrokerOrderContractError("broker result has no identifier or valid status")
        if order.status is OrderStatus.PENDING and (
            not isinstance(order.created_at, datetime) or order.created_at.utcoffset() is None
        ):
            raise BrokerOrderContractError(
                "pending broker result must have a timezone-aware creation time"
            )
        if not math.isfinite(float(order.filled_quantity)) or not (
            0 <= order.filled_quantity <= order.quantity
        ):
            raise BrokerOrderContractError("broker result has an invalid cumulative fill")
        if order.status is OrderStatus.FILLED and (
            not math.isclose(order.filled_quantity, order.quantity, rel_tol=0, abs_tol=1e-12)
            or order.filled_price is None
            or not math.isfinite(order.filled_price)
            or order.filled_price <= 0
            or not isinstance(order.filled_at, datetime)
            or order.filled_at.utcoffset() is None
        ):
            raise BrokerOrderContractError("filled broker result has incomplete fill evidence")


__all__ = [
    "BrokerOrderContractError",
    "CanonicalOrderRequest",
    "OrderValidationError",
    "UnsupportedOrderCapabilityError",
]
