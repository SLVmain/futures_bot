import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import time
from uuid import uuid4

from core.api_models import OpenPosition
from services.order_service import OrderService
from services.position_service import PositionService
from services.protection_service import ProtectionService
from services.trade_journal import CsvTradeJournal, JournalEvent


Notifier = Callable[[str, str | None], Awaitable[None]]
POSITION_UNAVAILABLE = object()


@dataclass(frozen=True)
class BreakEvenProposal:
    proposal_id: str
    position_id: str
    expires_at: float


class PositionMonitor:
    QUANTITY_TOLERANCE = Decimal("0.000000001")

    def __init__(
        self,
        orders: OrderService,
        positions: PositionService,
        protections: ProtectionService,
        journal: CsvTradeJournal,
        notifier: Notifier,
        market=None,
        taker_fee_rate: Decimal = Decimal("0.0006"),
        position_retry_delays: tuple[float, ...] = (
            0.5,
            1,
            2,
            4,
        ),
    ):
        self.orders = orders
        self.positions = positions
        self.protections = protections
        self.journal = journal
        self.notifier = notifier
        self.market = market
        self.taker_fee_rate = taker_fee_rate
        self.position_retry_delays = position_retry_delays
        self._plans_by_client_id = journal.load_pending_plans()
        self._tp_orders = journal.load_active_tp_orders()
        self._tp1_order_positions = {
            order_id: position_id
            for order_id, (position_id, tp_number)
            in self._tp_orders.items()
            if tp_number == 1
        }
        self._break_even_proposals = {}

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
        position_snapshot = None
        if channel == "tpsl" and status == "FILLED":
            position_snapshot = await self._position_after_execution(
                str(data.get("symbol", "")),
                str(data.get("positionId", "")),
            )
        event_type = self._journal_event_type(
            channel,
            data,
            status,
        )
        quantity = self._execution_quantity(channel, data)
        snapshot = (
            position_snapshot
            if position_snapshot not in (None, POSITION_UNAVAILABLE)
            else None
        )
        written = await asyncio.to_thread(
            self.journal.append,
            JournalEvent(
                event_type=event_type,
                status=status,
                symbol=str(data.get("symbol", "")),
                side=str(data.get("side", "")),
                order_type=str(data.get("type", "")),
                entry_price=str(
                    data.get("averagePrice", data.get("price", ""))
                ),
                quantity=quantity,
                leverage=str(data.get("leverage", "")),
                stop_loss=str(data.get("slPrice", "")),
                take_profit=str(data.get("tpPrice", "")),
                client_id=str(data.get("clientId", "")),
                order_id=str(data.get("orderId", "")),
                position_id=str(data.get("positionId", "")),
                pnl=str(
                    snapshot.realized_pnl
                    if snapshot is not None
                    else data.get(
                        "realizedPNL",
                        data.get("unrealizedPNL", ""),
                    )
                ),
                fee=str(
                    snapshot.fee
                    if snapshot is not None
                    else data.get("fee", "")
                ),
                funding=str(
                    snapshot.funding
                    if snapshot is not None
                    else data.get("funding", "")
                ),
                remaining_quantity=str(
                    snapshot.quantity
                    if snapshot is not None
                    else (
                        "0"
                        if (
                            channel == "position"
                            and data.get("event") == "CLOSE"
                        )
                        else data.get("remainingQty", "")
                    )
                ),
                source_event_id=event_id,
            ),
        )
        if not written:
            return
        if channel == "position" and data.get("event") == "CLOSE":
            await asyncio.to_thread(
                self.journal.append,
                self._trade_summary_event(message, data),
            )
        notification = await self._notification(
            channel,
            data,
            status,
            position_snapshot,
        )
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
            await self._request_break_even(data)

    async def _install_take_profits(self, data: dict) -> None:
        client_id = str(data.get("clientId", ""))
        plan = self._plans_by_client_id.get(client_id)
        if plan is None:
            return
        await self._install_plan(plan)

    async def _install_plan(self, plan) -> None:
        position = await self._wait_for_position(plan)
        if position is None:
            await self.notifier(
                "⚠️ Ордер исполнен, но позиция не появилась "
                "после повторных проверок. "
                "TP не выставлены; проверьте Bitunix."
            )
            return
        existing = await asyncio.to_thread(
            self.protections.get_pending_tp_sl,
            plan.symbol,
            position.position_id,
        )
        existing_take_profits = []
        for item in existing:
            if (
                str(item.get("positionId", ""))
                != position.position_id
                or not item.get("tpPrice")
            ):
                continue
            try:
                price = Decimal(str(item.get("tpPrice")))
                quantity = Decimal(str(item.get("tpQty")))
            except (InvalidOperation, TypeError):
                continue
            if quantity <= 0:
                continue
            existing_take_profits.append(
                (price, quantity, item)
            )
        position_quantity = Decimal(str(position.quantity))
        reserved_quantity = sum(
            (
                quantity
                for _, quantity, _ in existing_take_profits
            ),
            Decimal("0"),
        )
        if (
            reserved_quantity - position_quantity
            > self.QUANTITY_TOLERANCE
        ):
            await self.notifier(
                "⚠️ Объём активных TP превышает объём позиции: "
                f"{reserved_quantity} > {position_quantity}. "
                "Новые TP не будут выставлены."
            )
            return
        placed = 0
        recovered = 0
        failed = False
        used_existing_ids = set()
        client_id = next(
            (
                key
                for key, candidate
                in self._plans_by_client_id.items()
                if candidate.execution_id == plan.execution_id
            ),
            "",
        )
        for index, take_profit in enumerate(
            plan.take_profits,
            start=1,
        ):
            price = Decimal(str(take_profit.price))
            quantity = Decimal(str(take_profit.quantity))
            existing_match = next(
                (
                    item
                    for existing_price, existing_quantity, item
                    in existing_take_profits
                    if existing_price == price
                    and abs(existing_quantity - quantity)
                    <= self.QUANTITY_TOLERANCE
                    and str(item.get("id", item.get("orderId", "")))
                    not in used_existing_ids
                ),
                None,
            )
            if existing_match is not None:
                tp_order_id = str(
                    existing_match.get(
                        "id",
                        existing_match.get("orderId", ""),
                    )
                )
                if not tp_order_id:
                    failed = True
                    await self.notifier(
                        "⚠️ Существующий TP найден, но его ID "
                        "отсутствует в ответе Bitunix. "
                        "Перенос SL после TP1 недоступен."
                    )
                    continue
                used_existing_ids.add(tp_order_id)
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
                self._tp_orders[tp_order_id] = (
                    position.position_id,
                    index,
                )
                recovered += 1
                continue
            conflicting = [
                existing_quantity
                for existing_price, existing_quantity, _
                in existing_take_profits
                if existing_price == price
            ]
            if conflicting:
                failed = True
                await self.notifier(
                    f"⚠️ TP{index} по цене {price} уже существует, "
                    "но его объём не совпадает с планом: "
                    f"Bitunix={','.join(map(str, conflicting))}, "
                    f"план={quantity}. Новый TP не выставлен."
                )
                continue
            if (
                reserved_quantity + quantity - position_quantity
                > self.QUANTITY_TOLERANCE
            ):
                failed = True
                await self.notifier(
                    f"⚠️ TP{index} не выставлен: суммарный объём "
                    f"{reserved_quantity + quantity} превышает "
                    f"остаток позиции {position_quantity}."
                )
                continue
            try:
                tp_order_id = await asyncio.to_thread(
                    self.protections.place_take_profit,
                    plan.symbol,
                    position.position_id,
                    take_profit.price,
                    take_profit.quantity,
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
                self._tp_orders[tp_order_id] = (
                    position.position_id,
                    index,
                )
                placed += 1
                reserved_quantity += quantity
            except Exception as error:
                failed = True
                await self.notifier(
                    "⚠️ Не удалось выставить TP "
                    f"{take_profit.price}: "
                    f"{type(error).__name__}: {error}. "
                    "Проверьте позицию на Bitunix."
                )
        if placed:
            await self.notifier(
                f"✅ Добавлено частичных TP: {placed}; "
                f"позиция {position.position_id}"
            )
        if recovered:
            await self.notifier(
                f"✅ Восстановлены ID существующих TP: {recovered}; "
                "контроль TP1 активен"
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

    async def _wait_for_position(self, plan):
        delays = (0, *self.position_retry_delays)
        for attempt, delay in enumerate(delays):
            if delay:
                await asyncio.sleep(delay)
            positions = await asyncio.to_thread(
                self.positions.get_open_positions,
                plan.symbol,
            )
            matching = tuple(
                position
                for position in positions
                if position.side == plan.side.value
            )
            if matching:
                return matching[0]
            if attempt < len(delays) - 1:
                continue
        return None

    async def _request_break_even(self, data: dict) -> None:
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
        proposal = BreakEvenProposal(
            proposal_id=uuid4().hex,
            position_id=position_id,
            expires_at=time.time() + 300,
        )
        self._break_even_proposals[proposal.proposal_id] = proposal
        current_stops = ", ".join(
            str(item.get("slPrice"))
            for item in stop_orders
        )
        await self.notifier(
            "🎯 TP1 исполнен\n\n"
            f"Позиция: {position.side} {position.symbol}\n"
            f"ID: {position.position_id}\n"
            f"Остаток: {position.quantity}\n"
            f"Плечо: {position.leverage}x\n"
            f"Средняя цена: {position.average_open_price}\n"
            f"Текущий SL: {current_stops}\n"
            f"Предлагаемый SL: {break_even}\n"
            f"Realized PnL: {position.realized_pnl}\n"
            f"Unrealized PnL: {position.unrealized_pnl}\n"
            f"Комиссии: {position.fee}\n"
            f"Funding: {position.funding}\n\n"
            "Перенести SL в fee-aware безубыток?",
            proposal.proposal_id,
        )

    async def confirm_break_even(
        self,
        proposal_id: str,
        confirm: bool,
        now: float | None = None,
    ) -> str:
        proposal = self._break_even_proposals.pop(
            proposal_id,
            None,
        )
        if proposal is None:
            raise ValueError(
                "Подтверждение не найдено или уже использовано"
            )
        current_time = time.time() if now is None else now
        if current_time >= proposal.expires_at:
            raise ValueError("Время подтверждения истекло")
        if not confirm:
            return "SL оставлен без изменений"

        positions = await asyncio.to_thread(
            self.positions.get_open_positions,
            None,
            proposal.position_id,
        )
        if not positions:
            raise ValueError("Позиция уже закрыта")
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
            position.position_id,
        )
        stop_orders = [
            item
            for item in protections
            if item.get("slPrice") and item.get("id")
        ]
        if not stop_orders:
            raise ValueError("Активный SL не найден")
        for stop_order in stop_orders:
            await asyncio.to_thread(
                self.protections.modify_stop_loss,
                str(stop_order["id"]),
                str(break_even),
                position.quantity,
            )
        await asyncio.to_thread(
            self.journal.append,
            JournalEvent(
                event_type="break_even",
                status="COMPLETED",
                symbol=position.symbol,
                position_id=position.position_id,
                stop_loss=str(break_even),
                source_event_id=(
                    f"break-even:{position.position_id}"
                ),
            ),
        )
        self._tp1_order_positions = {
            order_id: position_id
            for order_id, position_id
            in self._tp1_order_positions.items()
            if position_id != position.position_id
        }
        return (
            "SL перенесён в fee-aware безубыток: "
            f"{break_even}"
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

    async def _notification(
        self,
        channel: str,
        data: dict,
        status: str,
        position_snapshot=None,
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
                if status == "FILLED":
                    side = OpenPosition.normalize_side(
                        data.get("side", "")
                    )
                    return (
                        "✅ Исполнен входной ордер\n\n"
                        f"Позиция: {side} {symbol}\n"
                        f"Тип: {data.get('type', '')}\n"
                        "Средняя цена: "
                        f"{data.get('averagePrice', '')}\n"
                        "Исполнено: "
                        f"{data.get('dealAmount', data.get('qty', ''))}\n"
                        f"Комиссия: {data.get('fee', '')}\n"
                        f"ID: {data.get('orderId', '')}"
                    )
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
                order_id = str(data.get("orderId", ""))
                tp_details = self._tp_orders.get(order_id)
                if kind == "TP" and tp_details is not None:
                    kind = f"TP{tp_details[1]}"
                position = position_snapshot
                if position is None:
                    position = await self._position_after_execution(
                        symbol,
                        str(data.get("positionId", "")),
                    )
                side = OpenPosition.normalize_side(
                    data.get("side", "")
                )
                trigger_price = data.get(
                    "tpPrice",
                ) or data.get(
                    "slPrice",
                    "",
                )
                executed_quantity = data.get(
                    "tpQty",
                ) or data.get(
                    "slQty",
                    "",
                )
                if position is POSITION_UNAVAILABLE:
                    position_lines = (
                        "Остаток: не удалось получить\n"
                    )
                elif position is None:
                    position_lines = (
                        "Остаток: позиция закрыта\n"
                    )
                else:
                    position_lines = (
                        f"Остаток: {position.quantity}\n"
                        f"Realized PnL: {position.realized_pnl}\n"
                        f"Unrealized PnL: {position.unrealized_pnl}\n"
                        f"Комиссии: {position.fee}\n"
                        f"Funding: {position.funding}\n"
                    )
                icon = "🎯" if kind.startswith("TP") else "🛑"
                return (
                    f"{icon} Исполнен {kind}\n\n"
                    f"Позиция: {side} {symbol}\n"
                    f"Триггер: {trigger_price}\n"
                    f"Закрыто: {executed_quantity}\n"
                    f"{position_lines}"
                    f"ID: {order_id}"
                )
            if status == "CANCELED":
                return (
                    f"⚠️ Защитный TP/SL отменён: {symbol}, "
                    f"позиция {data.get('positionId', '')}"
                )
        return None

    def _journal_event_type(
        self,
        channel: str,
        data: dict,
        status: str,
    ) -> str:
        if (
            channel == "order"
            and status == "FILLED"
            and str(data.get("clientId", ""))
            in self._plans_by_client_id
        ):
            return "ENTRY"
        if channel == "tpsl" and status == "FILLED":
            order_id = str(data.get("orderId", ""))
            tp_details = self._tp_orders.get(order_id)
            if data.get("tpPrice"):
                return (
                    f"TP{tp_details[1]}"
                    if tp_details is not None
                    else "TP"
                )
            if data.get("slPrice"):
                return "SL"
        if channel == "position" and data.get("event") == "CLOSE":
            return "POSITION_CLOSE"
        return channel

    @staticmethod
    def _execution_quantity(channel: str, data: dict) -> str:
        if channel == "tpsl":
            return str(
                data.get("tpQty")
                or data.get("slQty")
                or data.get("qty", "")
            )
        return str(
            data.get("dealAmount", data.get("qty", ""))
        )

    @staticmethod
    def _trade_summary_event(
        message: dict,
        data: dict,
    ) -> JournalEvent:
        realized = str(data.get("realizedPNL", ""))
        fee = str(data.get("fee", ""))
        funding = str(data.get("funding", ""))
        net_pnl = ""
        try:
            net_pnl = str(
                Decimal(realized or "0")
                + Decimal(funding or "0")
                - abs(Decimal(fee or "0"))
            )
        except InvalidOperation:
            pass
        position_id = str(data.get("positionId", ""))
        timestamp = str(message.get("ts", data.get("mtime", "")))
        return JournalEvent(
            event_type="TRADE_SUMMARY",
            status="CLOSED",
            symbol=str(data.get("symbol", "")),
            side=OpenPosition.normalize_side(data.get("side", "")),
            entry_price=str(
                data.get(
                    "avgOpenPrice",
                    data.get("averagePrice", ""),
                )
            ),
            quantity=str(data.get("qty", "")),
            leverage=str(data.get("leverage", "")),
            position_id=position_id,
            pnl=realized,
            fee=fee,
            funding=funding,
            net_pnl=net_pnl,
            remaining_quantity="0",
            source_event_id=(
                f"trade-summary:{position_id}:{timestamp}"
            ),
        )

    async def _position_after_execution(
        self,
        symbol: str,
        position_id: str,
    ):
        try:
            positions = await asyncio.to_thread(
                self.positions.get_open_positions,
                symbol or None,
                position_id or None,
            )
        except Exception:
            return POSITION_UNAVAILABLE
        return positions[0] if positions else None
