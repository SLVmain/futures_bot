from core.api_models import (
    BatchOrderFailure,
    BatchOrderResult,
    BatchPlacedOrder,
)
from models.signal import OrderSide
from models.trade import PlannedTakeProfit, TradePlan
from services.execution_service import ExecutionService


class FakeOrderService:
    def __init__(self):
        self.calls = []

    def place_batch_orders(self, symbol, orders):
        self.calls.append((symbol, orders))
        return BatchOrderResult(
            placed=tuple(
                BatchPlacedOrder(
                    order_id=f"order-{index}",
                    client_id=order["clientId"],
                )
                for index, order in enumerate(orders, start=1)
            ),
            failed=(),
            simulated=True,
        )


class FakeAccountService:
    def __init__(self):
        self.calls = []

    def ensure_leverage(self, symbol, leverage):
        self.calls.append((symbol, leverage))


class PartiallyFailingOrderService(FakeOrderService):
    def place_batch_orders(self, symbol, orders):
        self.calls.append((symbol, orders))
        return BatchOrderResult(
            placed=(
                BatchPlacedOrder("order-1", orders[0]["clientId"]),
            ),
            failed=tuple(
                BatchOrderFailure(
                    order["clientId"],
                    "10001",
                    "Rejected",
                )
                for order in orders[1:]
            ),
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
    assert len(result.orders) == 3
    assert len(orders.calls) == 1
    symbol, batch = orders.calls[0]
    assert symbol == "BTCUSDT"
    assert [item["qty"] for item in batch] == ["1", "0.5", "0.5"]
    assert [item["tpPrice"] for item in batch] == [
        "55",
        "60",
        "65",
    ]
    assert [item["slPrice"] for item in batch] == ["45", "45", "45"]
    assert all(item["slStopType"] == "MARK_PRICE" for item in batch)
    assert all(item["slOrderType"] == "MARKET" for item in batch)
    assert [item["clientId"] for item in batch] == [
        f"bot-{plan.execution_id[:20]}-1",
        f"bot-{plan.execution_id[:20]}-2",
        f"bot-{plan.execution_id[:20]}-3",
    ]
    assert all(item["orderType"] == "MARKET" for item in batch)
    assert all("price" not in item for item in batch)
    assert account.calls == [("BTCUSDT", 10)]


def test_rejects_plan_outside_entry_range_without_orders():
    orders = FakeOrderService()

    result = ExecutionService(orders).execute(make_plan(in_range=False))

    assert result.success is False
    assert result.error == (
        "Цена вне диапазона, но цена лимитного ордера не задана"
    )
    assert orders.calls == []


def test_rejects_api_unsupported_manual_plan_without_side_effects():
    orders = FakeOrderService()
    account = FakeAccountService()
    plan = TradePlan(**{
        **make_plan().__dict__,
        "api_execution_supported": False,
    })

    result = ExecutionService(orders, account).execute(plan)

    assert result.success is False
    assert "только для ручного размещения" in result.error
    assert orders.calls == []
    assert account.calls == []


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
    _, batch = orders.calls[0]
    assert all(item["price"] == "50" for item in batch)
    assert [item["qty"] for item in batch] == ["1", "0.5", "0.5"]
    assert all(item["orderType"] == "LIMIT" for item in batch)


def test_batch_partial_failure_reports_every_rejected_part():
    orders = PartiallyFailingOrderService()

    result = ExecutionService(orders).execute(make_plan())
    data = result.to_dict()

    assert result.status == "PARTIAL"
    assert data["success"] is False
    assert data["partial"] is True
    assert len(data["orders"]) == 1
    assert data["failed_orders"] == [
        {"tp": 2, "error": "10001: Rejected"},
        {"tp": 3, "error": "10001: Rejected"},
    ]
    assert len(orders.calls) == 1
