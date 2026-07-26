from dataclasses import dataclass, field
import time
from uuid import uuid4

from .signal import OrderSide


@dataclass(frozen=True)
class PlannedTakeProfit:
    price: float
    quantity: float


@dataclass(frozen=True)
class TradePlan:
    symbol: str
    side: OrderSide
    entry_min: float
    entry_max: float
    current_price: float
    in_range: bool
    total_quantity: float
    stop_loss: float
    take_profits: tuple[PlannedTakeProfit, ...]
    leverage: int
    risk_percent: float
    risk_budget: float
    execution_id: str = field(
        default_factory=lambda: uuid4().hex
    )

    @property
    def estimated_stop_loss(self) -> float:
        return (
            abs(self.current_price - self.stop_loss)
            * self.total_quantity
        )

    @property
    def margin_required(self) -> float:
        return (
            self.total_quantity
            * self.current_price
            / self.leverage
        )

    def to_order_info(self) -> dict:
        return {
            "ready": True,
            "symbol": self.symbol,
            "side": self.side.value,
            "entry_min": self.entry_min,
            "entry_max": self.entry_max,
            "current_price": self.current_price,
            "in_range": self.in_range,
            "total_quantity": self.total_quantity,
            "stop_loss": self.stop_loss,
            "take_profits": [
                item.price for item in self.take_profits
            ],
            "tp_quantities": [
                item.quantity for item in self.take_profits
            ],
            "leverage": self.leverage,
            "risk_percent": self.risk_percent,
            "risk_budget": self.risk_budget,
            "estimated_stop_loss": self.estimated_stop_loss,
            "margin_required": self.margin_required,
        }


@dataclass(frozen=True)
class PlacedOrder:
    tp_number: int
    price: float
    quantity: float
    order_id: str
    simulated: bool

    def to_dict(self) -> dict:
        return {
            "tp": self.tp_number,
            "price": self.price,
            "qty": self.quantity,
            "id": self.order_id,
            "simulated": self.simulated,
        }


@dataclass(frozen=True)
class FailedOrder:
    tp_number: int
    error: str

    def to_dict(self) -> dict:
        return {
            "tp": self.tp_number,
            "error": self.error,
        }


@dataclass(frozen=True)
class TradeExecutionResult:
    success: bool
    orders: tuple[PlacedOrder, ...] = ()
    failed_orders: tuple[FailedOrder, ...] = ()
    stop_loss: float | None = None
    simulated: bool = False
    error: str | None = None

    @property
    def status(self) -> str:
        if self.success and not self.failed_orders:
            return "SUCCESS"
        if self.orders:
            return "PARTIAL"
        return "FAILED"

    def to_dict(self) -> dict:
        if not self.success:
            return {
                "success": False,
                "status": self.status,
                "partial": bool(self.orders),
                "orders": [
                    order.to_dict() for order in self.orders
                ],
                "failed_orders": [
                    order.to_dict()
                    for order in self.failed_orders
                ],
                "stop_loss": self.stop_loss,
                "simulated": self.simulated,
                "error": self.error,
            }
        return {
            "success": True,
            "status": self.status,
            "partial": False,
            "orders": [order.to_dict() for order in self.orders],
            "failed_orders": [],
            "stop_loss": self.stop_loss,
            "simulated": self.simulated,
        }


class TradePlanningError(Exception):
    pass


@dataclass(frozen=True)
class TradeProposal:
    proposal_id: str
    plan: TradePlan
    created_at: float
    expires_at: float

    @classmethod
    def create(
        cls,
        plan: TradePlan,
        ttl_seconds: int = 300,
        now: float | None = None,
    ) -> "TradeProposal":
        created_at = time.time() if now is None else now
        return cls(
            proposal_id=uuid4().hex,
            plan=plan,
            created_at=created_at,
            expires_at=created_at + ttl_seconds,
        )

    def is_expired(self, now: float | None = None) -> bool:
        current_time = time.time() if now is None else now
        return current_time >= self.expires_at
