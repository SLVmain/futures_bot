import asyncio
from collections.abc import Awaitable, Callable

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
    ):
        self.orders = orders
        self.positions = positions
        self.protections = protections
        self.journal = journal
        self.notifier = notifier

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
