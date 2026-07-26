from core.api_models import OrderResult
from models.signal import OrderSide
from models.trade import PlannedTakeProfit, TradePlan
from services.execution_service import ExecutionService


class FakeOrderService:
    def __init__(self):
        self.calls = []

    def open_long(self, **kwargs):
        self.calls.append(kwargs)
        return OrderResult(
            code=0,
            message="DRY_RUN",
            order_id=f"order-{len(self.calls)}",
            client_id=None,
            simulated=True,
        )


class FakeAccountService:
    def __init__(self):
        self.calls = []

    def ensure_leverage(self, symbol, leverage):
        self.calls.append((symbol, leverage))


class PartiallyFailingOrderService(FakeOrderService):
    def open_long(self, **kwargs):
        self.calls.append(kwargs)
        return OrderResult(
            code=10001,
            message="Rejected",
            order_id=None,
            client_id=kwargs["client_id"],
            simulated=False,
        )


def make_plan(in_range=True):
    return TradePlan(
        symbol="BTCUSDT",
        side=OrderSide.LONG,
        entry_min=49,
        entry_max=51,
        current_price=50,
        in_range=in_range,
        total_quantity=2,
        stop_loss=45,
        take_profits=(
            PlannedTakeProfit(price=55, quantity=1),
            PlannedTakeProfit(price=60, quantity=0.5),
            PlannedTakeProfit(price=65, quantity=0.5),
        ),
        leverage=10,
        risk_percent=1,
        risk_budget=10,
    )


def test_executes_existing_plan_without_market_lookup():
    orders = FakeOrderService()
    account = FakeAccountService()
    plan = make_plan()

    result = ExecutionService(orders, account).execute(plan)

    assert result.success is True
    assert result.simulated is True
    assert len(result.orders) == 1
    assert [call["quantity"] for call in orders.calls] == [2]
    assert [call["client_id"] for call in orders.calls] == [
        f"bot-{plan.execution_id[:20]}-1",
    ]
    assert account.calls == [("BTCUSDT", 10)]


def test_rejects_plan_outside_entry_range_without_orders():
    orders = FakeOrderService()

    result = ExecutionService(orders).execute(make_plan(in_range=False))

    assert result.success is False
    assert result.error == (
        "Цена вне диапазона, но цена лимитного ордера не задана"
    )
    assert orders.calls == []


def test_outside_range_executes_limit_order_at_planned_price():
    orders = FakeOrderService()
    plan = make_plan(in_range=False)
    plan = TradePlan(
        **{
            **plan.__dict__,
            "limit_price": 50,
        }
    )

    result = ExecutionService(orders).execute(plan)

    assert result.success is True
    assert orders.calls[0]["price"] == 50
    assert orders.calls[0]["quantity"] == plan.total_quantity


def test_first_order_failure_is_not_partial():
    orders = PartiallyFailingOrderService()

    result = ExecutionService(orders).execute(make_plan())
    data = result.to_dict()

    assert result.status == "FAILED"
    assert data["success"] is False
    assert data["partial"] is False
    assert len(data["orders"]) == 0
    assert data["failed_orders"] == [
        {"tp": 1, "error": "Rejected"}
    ]
    assert len(orders.calls) == 1
