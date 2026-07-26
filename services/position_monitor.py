import asyncio
from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation

from services.order_service import OrderService
from services.position_service import PositionService
from services.protection_service import ProtectionService
from services.trade_journal import CsvTradeJournal, JournalEvent


Notifier = Callable[[str], Awaitable[None]]


class PositionMonitor:
    def __init__(
        self,
        orders: OrderService,
        positions: PositionService,
        protections: ProtectionService,
        journal: CsvTradeJournal,
        notifier: Notifier,
        market=None,
        taker_fee_rate: Decimal = Decimal("0.0006"),
    ):
        self.orders = orders
        self.positions = positions
        self.protections = protections
        self.journal = journal
        self.notifier = notifier
        self.market = market
        self.taker_fee_rate = taker_fee_rate
        self._plans_by_client_id = journal.load_pending_plans()
        self._tp1_order_positions = (
            journal.load_active_tp1_orders()
        )

    def register_plan(self, client_id: str, plan) -> None:
        self._plans_by_client_id[client_id] = plan
        self.journal.save_plan(client_id, plan)

    def discard_plan(self, client_id: str) -> None:
        self._plans_by_client_id.pop(client_id, None)
        self.journal.finish_plan(client_id, "FAILED")

    async def handle_event(self, message: dict) -> None:
        channel = str(message.get("ch", ""))
        data = message.get("data")
        if not isinstance(data, dict):
            return
        status = str(
            data.get(
                "orderStatus",
                data.get("status", data.get("event", "")),
            )
        )
        event_id = ":".join((
            channel,
            str(data.get("orderId", data.get("positionId", ""))),
            status,
            str(message.get("ts", data.get("mtime", ""))),
        ))
        written = await asyncio.to_thread(
            self.journal.append,
            JournalEvent(
                event_type=channel,
                status=status,
                symbol=str(data.get("symbol", "")),
                side=str(data.get("side", "")),
                order_type=str(data.get("type", "")),
                entry_price=str(
                    data.get("averagePrice", data.get("price", ""))
                ),
                quantity=str(
                    data.get("dealAmount", data.get("qty", ""))
                ),
                leverage=str(data.get("leverage", "")),
                stop_loss=str(data.get("slPrice", "")),
                take_profit=str(data.get("tpPrice", "")),
                client_id=str(data.get("clientId", "")),
                order_id=str(data.get("orderId", "")),
                position_id=str(data.get("positionId", "")),
                pnl=str(
                    data.get(
                        "realizedPNL",
                        data.get("unrealizedPNL", ""),
                    )
                ),
                source_event_id=event_id,
            ),
        )
        if not written:
            return
        notification = self._notification(channel, data, status)
        if notification:
            await self.notifier(notification)
        if channel == "order" and status == "FILLED":
            await self._install_take_profits(data)
        if channel == "position" and data.get("event") == "OPEN":
            for plan in tuple(
                self._plans_by_client_id.values()
            ):
                if plan.symbol == data.get("symbol"):
                    await self._install_plan(plan)
        if channel == "tpsl" and status == "FILLED":
            await self._move_stop_to_break_even(data)

    async def _install_take_profits(self, data: dict) -> None:
        client_id = str(data.get("clientId", ""))
        plan = self._plans_by_client_id.get(client_id)
        if plan is None:
            return
        await self._install_plan(plan)

    async def _install_plan(self, plan) -> None:
        positions = await asyncio.to_thread(
            self.positions.get_open_positions,
            plan.symbol,
        )
        matching = tuple(
            position
            for position in positions
            if position.side == plan.side.value
        )
        if not matching:
            await self.notifier(
                "⚠️ Ордер исполнен, но позиция ещё не найдена. "
                "TP не выставлены; проверьте Bitunix."
            )
            return
        position = matching[0]
        existing = await asyncio.to_thread(
            self.protections.get_pending_tp_sl,
            plan.symbol,
            position.position_id,
        )
        existing_prices = set()
        for item in existing:
            try:
                existing_prices.add(
                    Decimal(str(item.get("tpPrice")))
                )
            except (InvalidOperation, TypeError):
                pass
        placed = 0
        failed = False
        for index, take_profit in enumerate(
            plan.take_profits,
            start=1,
        ):
            if Decimal(str(take_profit.price)) in existing_prices:
                continue
            try:
                tp_order_id = await asyncio.to_thread(
                    self.protections.place_take_profit,
                    plan.symbol,
                    position.position_id,
                    take_profit.price,
                    take_profit.quantity,
                )
                client_id = next(
                    (
                        key
                        for key, candidate
                        in self._plans_by_client_id.items()
                        if candidate.execution_id
                        == plan.execution_id
                    ),
                    "",
                )
                self.journal.save_tp_order(
                    tp_order_id,
                    position.position_id,
                    client_id,
                    index,
                )
                if index == 1:
                    self._tp1_order_positions[tp_order_id] = (
                        position.position_id
                    )
                placed += 1
            except Exception as error:
                failed = True
                await self.notifier(
                    "⚠️ Не удалось выставить TP "
                    f"{take_profit.price}: "
                    f"{type(error).__name__}. "
                    "Проверьте позицию на Bitunix."
                )
        if placed:
            await self.notifier(
                f"✅ Добавлено частичных TP: {placed}; "
                f"позиция {position.position_id}"
            )
        if not failed:
            client_ids = [
                client_id
                for client_id, candidate in self._plans_by_client_id.items()
                if candidate.execution_id == plan.execution_id
            ]
            for client_id in client_ids:
                self.journal.finish_plan(client_id, "CONFIGURED")
                self._plans_by_client_id.pop(client_id, None)

    async def _move_stop_to_break_even(self, data: dict) -> None:
        order_id = str(data.get("orderId", ""))
        position_id = self._tp1_order_positions.get(order_id)
        if not position_id:
            return
        positions = await asyncio.to_thread(
            self.positions.get_open_positions,
            None,
            position_id,
        )
        if not positions:
            return
        position = positions[0]
        quote_precision = 8
        if self.market is not None:
            instrument = await asyncio.to_thread(
                self.market.get_trading_pair,
                position.symbol,
            )
            quote_precision = instrument.quote_precision
        break_even = ProtectionService.fee_aware_break_even(
            position,
            quote_precision,
            self.taker_fee_rate,
        )
        protections = await asyncio.to_thread(
            self.protections.get_pending_tp_sl,
            position.symbol,
            position_id,
        )
        stop_orders = [
            item
            for item in protections
            if item.get("slPrice") and item.get("id")
        ]
        if not stop_orders:
            await self.notifier(
                "⚠️ TP1 исполнен, но активный SL не найден. "
                "Проверьте позицию на Bitunix."
            )
            return
        try:
            for stop_order in stop_orders:
                await asyncio.to_thread(
                    self.protections.modify_stop_loss,
                    str(stop_order["id"]),
                    str(break_even),
                    position.quantity,
                )
        except Exception as error:
            await self.notifier(
                "⚠️ Не удалось перенести SL в безубыток: "
                f"{type(error).__name__}"
            )
            return
        await asyncio.to_thread(
            self.journal.append,
            JournalEvent(
                event_type="break_even",
                status="COMPLETED",
                symbol=position.symbol,
                position_id=position_id,
                order_id=order_id,
                stop_loss=str(break_even),
                source_event_id=f"break-even:{position_id}",
            ),
        )
        self._tp1_order_positions.pop(order_id, None)
        await self.notifier(
            "✅ TP1 исполнен. SL перенесён в безубыток: "
            f"{break_even} (с учётом комиссий)"
        )

    async def reconcile(self) -> None:
        orders, positions, protections = await asyncio.gather(
            asyncio.to_thread(self.orders.get_pending_orders),
            asyncio.to_thread(self.positions.get_open_positions),
            asyncio.to_thread(self.protections.get_pending_tp_sl),
        )
        await asyncio.to_thread(
            self.journal.append,
            JournalEvent(
                event_type="reconciliation",
                status="COMPLETED",
                quantity=str(len(positions)),
                order_id=str(len(orders)),
            ),
        )
        for position, missing in self.protections.unprotected_positions(
            positions,
            protections,
        ):
            await self.notifier(
                "⚠️ Позиция без защиты: "
                f"{position.side} {position.symbol}, "
                f"ID {position.position_id}. "
                f"Отсутствует: {', '.join(missing)}"
            )
        for client_id, plan in tuple(
            self._plans_by_client_id.items()
        ):
            detail = await asyncio.to_thread(
                self.orders.get_order_detail,
                client_id,
            )
            status = str(detail.get("status", ""))
            if status == "FILLED":
                await self._install_plan(plan)
            elif status in {"CANCELED", "PART_FILLED_CANCELED"}:
                self.discard_plan(client_id)

    @staticmethod
    def _notification(
        channel: str,
        data: dict,
        status: str,
    ) -> str | None:
        symbol = str(data.get("symbol", ""))
        if channel == "order":
            descriptions = {
                "FILLED": "Ордер исполнен",
                "CANCELED": "Ордер отменён",
                "PART_FILLED_CANCELED": (
                    "Ордер частично исполнен и отменён"
                ),
            }
            description = descriptions.get(status)
            if description:
                return (
                    f"ℹ️ {description}: {symbol}, "
                    f"ID {data.get('orderId', '')}"
                )
        if channel == "position":
            event = str(data.get("event", ""))
            if event == "OPEN":
                return (
                    f"✅ Позиция открыта: "
                    f"{data.get('side', '')} {symbol}"
                )
            if event == "CLOSE":
                return (
                    f"🏁 Позиция закрыта: {symbol}; "
                    f"PnL: {data.get('realizedPNL', '')}"
                )
        if channel == "tpsl":
            if status == "FILLED":
                has_tp = bool(data.get("tpPrice"))
                has_sl = bool(data.get("slPrice"))
                if has_tp and not has_sl:
                    kind = "TP"
                elif has_sl and not has_tp:
                    kind = "SL"
                else:
                    kind = "TP/SL"
                return f"🏁 Сработал {kind}: {symbol}"
            if status == "CANCELED":
                return (
                    f"⚠️ Защитный TP/SL отменён: {symbol}, "
                    f"позиция {data.get('positionId', '')}"
                )
        return None
