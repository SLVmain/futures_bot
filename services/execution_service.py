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

    @staticmethod
    def client_ids(plan: TradePlan) -> tuple[str, ...]:
        return tuple(
            f"bot-{plan.execution_id[:20]}-{index}"
            for index in range(1, len(plan.take_profits) + 1)
        )

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

        client_ids = self.client_ids(plan)
        side = (
            "BUY"
            if plan.side is OrderSide.LONG
            else "SELL"
        )
        order_specs = []
        take_profit_by_client_id = {}
        for index, (client_id, take_profit) in enumerate(
            zip(client_ids, plan.take_profits),
            start=1,
        ):
            spec = {
                "side": side,
                "qty": str(take_profit.quantity),
                "tradeSide": "OPEN",
                "orderType": plan.order_type,
                "effect": "GTC",
                "clientId": client_id,
                "reduceOnly": False,
                "tpPrice": str(take_profit.price),
                "tpStopType": "LAST_PRICE",
                "tpOrderType": "MARKET",
                "slPrice": str(plan.stop_loss),
                "slStopType": "LAST_PRICE",
                "slOrderType": "MARKET",
            }
            if plan.limit_price is not None:
                spec["price"] = str(plan.limit_price)
            order_specs.append(spec)
            take_profit_by_client_id[client_id] = (
                index,
                take_profit,
            )
            print(
                f"   TP{index}: {take_profit.price} | "
                f"{take_profit.quantity} | SL {plan.stop_loss}"
            )

        try:
            batch_result = self.order_service.place_batch_orders(
                plan.symbol,
                tuple(order_specs),
            )
        except BitunixError as error:
            return TradeExecutionResult(
                success=False,
                failed_orders=tuple(
                    FailedOrder(index, str(error))
                    for index in range(
                        1,
                        len(plan.take_profits) + 1,
                    )
                ),
                error="Не удалось разместить пакет входных ордеров",
            )

        placed_orders = []
        for item in batch_result.placed:
            details = take_profit_by_client_id.get(item.client_id)
            if details is None:
                continue
            index, take_profit = details
            print(f"   ✅ TP{index} ID: {item.order_id}")
            placed_orders.append(PlacedOrder(
                tp_number=index,
                price=take_profit.price,
                quantity=take_profit.quantity,
                order_id=item.order_id,
                simulated=batch_result.simulated,
            ))
        placed_orders.sort(key=lambda item: item.tp_number)

        failed_orders = []
        for item in batch_result.failed:
            details = take_profit_by_client_id.get(item.client_id)
            if details is None:
                continue
            index, _ = details
            error = ": ".join(
                part
                for part in (
                    item.error_code,
                    item.error_message,
                )
                if part
            ) or "Bitunix отклонил ордер"
            failed_orders.append(FailedOrder(index, error))
        reported_client_ids = {
            item.client_id for item in batch_result.placed
        } | {
            item.client_id for item in batch_result.failed
        }
        for client_id, (index, _) in (
            take_profit_by_client_id.items()
        ):
            if client_id not in reported_client_ids:
                failed_orders.append(FailedOrder(
                    index,
                    "Bitunix не вернул результат для части ордера",
                ))
        failed_orders.sort(key=lambda item: item.tp_number)
        simulated = batch_result.simulated

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

        print(
            f"\n✅ Размещено защищённых частей: "
            f"{len(placed_orders)}"
        )
        return TradeExecutionResult(
            success=True,
            orders=tuple(placed_orders),
            stop_loss=plan.stop_loss,
            simulated=simulated,
        )
