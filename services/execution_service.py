from models.signal import OrderSide
from models.trade import (
    FailedOrder,
    PlacedOrder,
    TradeExecutionResult,
    TradePlan,
)
from core.errors import BitunixError
from services.order_service import OrderService
from services.account_service import AccountService


class ExecutionService:
    def __init__(
        self,
        order_service: OrderService,
        account_service: AccountService | None = None,
    ):
        self.order_service = order_service
        self.account_service = account_service

    def execute(self, plan: TradePlan) -> TradeExecutionResult:
        if not plan.in_range and plan.limit_price is None:
            return TradeExecutionResult(
                success=False,
                error=(
                    "Цена вне диапазона, но цена лимитного "
                    "ордера не задана"
                ),
            )
        if self.account_service is not None:
            self.account_service.ensure_leverage(
                plan.symbol,
                plan.leverage,
            )

        print(f"\n🚀 ВХОД: {plan.side.value} {plan.symbol}")
        print(
            f"   Плечо: {plan.leverage}x | "
            f"Риск: {plan.risk_percent}%"
        )
        print(f"   Общий объём: {plan.total_quantity}")
        print(
            f"   Ордер: {plan.order_type} | "
            f"Цена входа: {plan.planned_entry_price} | "
            f"SL: {plan.stop_loss}"
        )

        placed_orders = []
        failed_orders = []
        simulated = True

        for index, take_profit in enumerate(
            plan.take_profits[:1],
            start=1,
        ):
            print(
                "   TP будут добавлены после подтверждённого "
                "исполнения входа"
            )

            method = (
                self.order_service.open_long
                if plan.side is OrderSide.LONG
                else self.order_service.open_short
            )
            try:
                result = method(
                    symbol=plan.symbol,
                    quantity=plan.total_quantity,
                    price=plan.limit_price,
                    sl_price=plan.stop_loss,
                    tp_price=None,
                    client_id=(
                        f"bot-{plan.execution_id[:20]}-{index}"
                    ),
                )
            except BitunixError as error:
                failed_orders.append(
                    FailedOrder(index, str(error))
                )
                break

            if result.success:
                order_id = result.order_id or "unknown"
                simulated = simulated and result.simulated
                print(f"   ✅ ID: {order_id}")
                placed_orders.append(
                    PlacedOrder(
                        tp_number=index,
                        price=take_profit.price,
                        quantity=plan.total_quantity,
                        order_id=order_id,
                        simulated=result.simulated,
                    )
                )
            else:
                print(f"   ❌ {result.message}")
                failed_orders.append(
                    FailedOrder(index, result.message)
                )
                break

        if not placed_orders:
            return TradeExecutionResult(
                success=False,
                failed_orders=tuple(failed_orders),
                error="Не удалось разместить ордера",
            )
        if failed_orders:
            return TradeExecutionResult(
                success=False,
                orders=tuple(placed_orders),
                failed_orders=tuple(failed_orders),
                stop_loss=plan.stop_loss,
                simulated=simulated,
                error="Позиция открыта частично",
            )

        print(f"\n✅ Размещено: {len(placed_orders)} ордеров")
        return TradeExecutionResult(
            success=True,
            orders=tuple(placed_orders),
            stop_loss=plan.stop_loss,
            simulated=simulated,
        )
