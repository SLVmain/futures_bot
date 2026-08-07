import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
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
    trigger_position_id: str
    position_ids: tuple[str, ...]
    expires_at: float


@dataclass
class EntryFillGroup:
    expected_client_ids: set[str]
    symbol: str = ""
    side: str = ""
    filled_client_ids: set[str] = field(default_factory=set)
    verified_client_ids: set[str] = field(default_factory=set)
    quantity: Decimal = Decimal("0")
    notional: Decimal = Decimal("0")
    fee: Decimal = Decimal("0")


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
        auto_move_stop_loss_on_tp1: bool = False,
    ):
        self.orders = orders
        self.positions = positions
        self.protections = protections
        self.journal = journal
        self.notifier = notifier
        self.market = market
        self.taker_fee_rate = taker_fee_rate
        self.position_retry_delays = position_retry_delays
        self.auto_move_stop_loss_on_tp1 = auto_move_stop_loss_on_tp1
        self._plans_by_client_id = journal.load_pending_plans()
        self._tp_numbers_by_client_id = {
            client_id: self._tp_number_from_client_id(client_id)
            for client_id in self._plans_by_client_id
        }
        self._tp_orders = journal.load_active_tp_orders()
        self._tp_order_client_ids = (
            journal.load_active_tp_order_client_ids()
        )
        self._tp_order_prices = journal.load_tp_order_prices()
        self._tp1_order_positions = {
            order_id: position_id
            for order_id, (position_id, tp_number)
            in self._tp_orders.items()
            if tp_number == 1
        }
        self._break_even_proposals = {}
        self._pending_plan_notifications: set[str] = set()
        self._entry_fill_groups: dict[str, EntryFillGroup] = {}
        self._recent_bot_open_symbols: dict[str, float] = {}
        for client_id, plan in self._plans_by_client_id.items():
            self._register_entry_notification(client_id, plan.symbol)

    def register_plan(
        self,
        client_id: str,
        plan,
        *,
        persist: bool = True,
        tp_number: int | None = None,
    ) -> None:
        if tp_number is not None:
            take_profit = plan.take_profits[tp_number - 1]
            plan = replace(
                plan,
                total_quantity=take_profit.quantity,
                take_profits=(take_profit,),
            )
            self._tp_numbers_by_client_id[client_id] = tp_number
        self._plans_by_client_id[client_id] = plan
        self._register_entry_notification(client_id, plan.symbol)
        if persist:
            self.journal.save_plan(client_id, plan)

    def discard_plan(self, client_id: str) -> None:
        self._plans_by_client_id.pop(client_id, None)
        self._tp_numbers_by_client_id.pop(client_id, None)
        self._discard_entry_notification(client_id)
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
            await self._verify_attached_protections(data)
        if channel == "position" and data.get("event") == "OPEN":
            for client_id, plan in tuple(
                self._plans_by_client_id.items()
            ):
                if plan.symbol == data.get("symbol"):
                    await self._verify_plan_protections(
                        plan,
                        client_id,
                    )
        if channel == "tpsl" and status == "FILLED":
            await self._handle_take_profit_stop_move(data)

    async def _verify_attached_protections(self, data: dict) -> None:
        client_id = str(data.get("clientId", ""))
        plan = self._plans_by_client_id.get(client_id)
        if plan is None:
            return
        await self._verify_plan_protections(plan, client_id)

    async def _verify_plan_protections(
        self,
        plan,
        client_id: str | None = None,
    ) -> None:
        position = await self._wait_for_position(plan)
        if position is None:
            await self.notifier(
                "⚠️ Ордер исполнен, но позиция не появилась "
                "после повторных проверок. "
                "Проверьте вход и прикреплённые TP/SL на Bitunix."
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
                "Мониторинг не будет изменять ордера."
            )
            return
        recovered = 0
        failed = False
        used_existing_ids = set()
        if client_id is None:
            client_id = next(
                (
                    key
                    for key, candidate
                    in self._plans_by_client_id.items()
                    if candidate is plan
                ),
                "",
            )
        first_tp_number = self._tp_numbers_by_client_id.get(
            client_id,
            1,
        )
        for index, take_profit in enumerate(
            plan.take_profits,
            start=first_tp_number,
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
                    str(price),
                )
                self._tp_order_client_ids[tp_order_id] = client_id
                self._tp_order_prices[tp_order_id] = price
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
                    f"план={quantity}. "
                    "Мониторинг не изменял ордера."
                )
                continue
            if (
                reserved_quantity + quantity - position_quantity
                > self.QUANTITY_TOLERANCE
            ):
                failed = True
                await self.notifier(
                    f"⚠️ TP{index}: суммарный объём "
                    f"{reserved_quantity + quantity} превышает "
                    f"остаток позиции {position_quantity}. "
                    "Мониторинг не изменял ордера."
                )
                continue
            failed = True
            await self.notifier(
                f"⚠️ Прикреплённый TP{index} не найден: "
                f"цена {take_profit.price}, "
                f"объём {take_profit.quantity}. "
                "Мониторинг не создаёт ордера автоматически; "
                "проверьте Bitunix."
            )
        if recovered:
            protection_message = self._mark_protection_verified(
                client_id,
            )
            if protection_message:
                await self.notifier(protection_message)
            elif not self._entry_group(client_id):
                await self.notifier(
                    f"✅ Проверены прикреплённые TP: {recovered}; "
                    f"контроль TP{first_tp_number} активен"
                )
        if not failed:
            if client_id:
                self.journal.finish_plan(client_id, "CONFIGURED")
                self._plans_by_client_id.pop(client_id, None)
                self._tp_numbers_by_client_id.pop(client_id, None)

    @staticmethod
    def _tp_number_from_client_id(client_id: str) -> int:
        try:
            number = int(client_id.rsplit("-", 1)[1])
        except (IndexError, ValueError):
            return 1
        return number if number > 0 else 1

    async def _wait_for_position(self, plan):
        delays = (0, *self.position_retry_delays)
        fallback = None
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
            expected_quantity = Decimal(str(plan.total_quantity))
            quantity_matches = tuple(
                position
                for position in matching
                if abs(
                    Decimal(str(position.quantity))
                    - expected_quantity
                ) <= self.QUANTITY_TOLERANCE
            )
            if len(quantity_matches) == 1:
                return quantity_matches[0]
            if quantity_matches:
                fallback = quantity_matches[0]
                expected_take_profits = {
                    (
                        Decimal(str(take_profit.price)),
                        Decimal(str(take_profit.quantity)),
                    )
                    for take_profit in plan.take_profits
                }
                for position in quantity_matches:
                    protections = await asyncio.to_thread(
                        self.protections.get_pending_tp_sl,
                        plan.symbol,
                        position.position_id,
                    )
                    if any(
                        self._protection_matches(
                            item,
                            position.position_id,
                            expected_take_profits,
                        )
                        for item in protections
                    ):
                        return position
            elif matching and len(plan.take_profits) > 1:
                # Compatibility with plans saved by older versions,
                # where all partial entries shared one aggregate plan.
                return matching[0]
            if attempt < len(delays) - 1:
                continue
        return fallback

    def _protection_matches(
        self,
        item: dict,
        position_id: str,
        expected_take_profits: set[tuple[Decimal, Decimal]],
    ) -> bool:
        if (
            str(item.get("positionId", "")) != position_id
            or not item.get("tpPrice")
        ):
            return False
        try:
            price = Decimal(str(item.get("tpPrice")))
            quantity = Decimal(str(item.get("tpQty")))
        except (InvalidOperation, TypeError):
            return False
        return any(
            price == expected_price
            and abs(quantity - expected_quantity)
            <= self.QUANTITY_TOLERANCE
            for expected_price, expected_quantity
            in expected_take_profits
        )

    async def _request_break_even(self, data: dict) -> None:
        order_id = str(data.get("orderId", ""))
        position_id = self._tp1_order_positions.get(order_id)
        if not position_id:
            return
        client_id = self._tp_order_client_ids.get(order_id, "")
        execution_prefix = self._execution_client_prefix(client_id)
        tracked_position_ids = {
            tracked_position_id
            for tracked_order_id, (tracked_position_id, tp_number)
            in self._tp_orders.items()
            if tp_number > 1
            and self._execution_client_prefix(
                self._tp_order_client_ids.get(tracked_order_id, "")
            ) == execution_prefix
            and execution_prefix
        }
        if not tracked_position_ids:
            # Compatibility with journal entries created before client IDs
            # were persisted for break-even grouping.
            tracked_position_ids = {position_id}
        open_positions = await asyncio.to_thread(
            self.positions.get_open_positions,
            str(data.get("symbol", "")) or None,
        )
        positions = tuple(
            position
            for position in open_positions
            if position.position_id in tracked_position_ids
        )
        if not positions:
            await self.notifier(
                "ℹ️ TP1 исполнен, но открытых частей этого сигнала "
                "не осталось. Перенос SL не требуется."
            )
            return
        quote_precision = 8
        if self.market is not None:
            instrument = await asyncio.to_thread(
                self.market.get_trading_pair,
                positions[0].symbol,
            )
            quote_precision = instrument.quote_precision
        proposal_lines = []
        for position in positions:
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
                if (
                    str(item.get("positionId", ""))
                    == position.position_id
                    and item.get("slPrice")
                    and item.get("id")
                )
            ]
            if not stop_orders:
                await self.notifier(
                    "⚠️ TP1 исполнен, но активный SL не найден "
                    f"для позиции {position.position_id}. "
                    "Ни один SL не изменён; проверьте Bitunix."
                )
                return
            current_stops = ", ".join(
                str(item.get("slPrice"))
                for item in stop_orders
            )
            proposal_lines.append(
                f"• {position.position_id}: объём {position.quantity}, "
                f"SL {current_stops} → {break_even}"
            )
        proposal = BreakEvenProposal(
            proposal_id=uuid4().hex,
            trigger_position_id=position_id,
            position_ids=tuple(
                position.position_id for position in positions
            ),
            expires_at=time.time() + 300,
        )
        self._break_even_proposals[proposal.proposal_id] = proposal
        if self.auto_move_stop_loss_on_tp1:
            try:
                result = await self.confirm_break_even(
                    proposal.proposal_id,
                    True,
                )
            except Exception as error:
                await self.notifier(
                    "⚠️ TP1 исполнен, но автоматический перенос "
                    "SL в безубыток не выполнен: "
                    f"{type(error).__name__}. "
                    "Проверьте оставшиеся позиции и SL на Bitunix."
                )
                return
            await self.notifier(
                "🎯 TP1 исполнен\n\n"
                "✅ Автоматический перенос SL выполнен\n"
                f"{result}"
            )
            return
        await self.notifier(
            "🎯 TP1 исполнен\n\n"
            f"Сигнал: {positions[0].side} {positions[0].symbol}\n"
            f"Оставшихся позиций: {len(positions)}\n\n"
            + "\n".join(proposal_lines)
            + "\n\nПеренести SL всех оставшихся частей "
            "в их fee-aware безубыток?",
            proposal.proposal_id,
        )

    async def _handle_take_profit_stop_move(self, data: dict) -> None:
        order_id = str(data.get("orderId", ""))
        tracked = self._tp_orders.get(order_id)
        if tracked is None:
            if order_id in self._tp1_order_positions:
                await self._request_break_even(data)
            return
        _, tp_number = tracked
        if tp_number == 1:
            await self._request_break_even(data)
            return
        if not self.auto_move_stop_loss_on_tp1:
            return
        try:
            await self._move_stop_to_previous_take_profit(
                data,
                tp_number,
            )
        except Exception as error:
            await self.notifier(
                f"⚠️ TP{tp_number} исполнен, но автоматический "
                "перенос SL не выполнен: "
                f"{type(error).__name__}. "
                "Проверьте оставшиеся позиции и SL на Bitunix."
            )

    async def _move_stop_to_previous_take_profit(
        self,
        data: dict,
        tp_number: int,
    ) -> None:
        order_id = str(data.get("orderId", ""))
        client_id = self._tp_order_client_ids.get(order_id, "")
        execution_prefix = self._execution_client_prefix(client_id)
        if not execution_prefix:
            raise ValueError("Не найдена группа сигнала")
        previous_order_id = next(
            (
                tracked_order_id
                for tracked_order_id, (_, tracked_tp_number)
                in self._tp_orders.items()
                if tracked_tp_number == tp_number - 1
                and self._execution_client_prefix(
                    self._tp_order_client_ids.get(
                        tracked_order_id,
                        "",
                    )
                ) == execution_prefix
            ),
            None,
        )
        target_stop = self._tp_order_prices.get(previous_order_id or "")
        if target_stop is None:
            raise ValueError("Не найдена цена предыдущего тейка")
        remaining_position_ids = {
            position_id
            for tracked_order_id, (position_id, tracked_tp_number)
            in self._tp_orders.items()
            if tracked_tp_number > tp_number
            and self._execution_client_prefix(
                self._tp_order_client_ids.get(tracked_order_id, "")
            ) == execution_prefix
        }
        if not remaining_position_ids:
            return
        open_positions = await asyncio.to_thread(
            self.positions.get_open_positions,
            str(data.get("symbol", "")) or None,
        )
        positions = tuple(
            position
            for position in open_positions
            if position.position_id in remaining_position_ids
        )
        if not positions:
            return
        changes = []
        protected_positions = set()
        for position in positions:
            protections = await asyncio.to_thread(
                self.protections.get_pending_tp_sl,
                position.symbol,
                position.position_id,
            )
            stop_orders = [
                item
                for item in protections
                if (
                    str(item.get("positionId", ""))
                    == position.position_id
                    and item.get("slPrice")
                    and item.get("id")
                )
            ]
            if not stop_orders:
                raise ValueError(
                    "Активный SL не найден для позиции "
                    f"{position.position_id}"
                )
            for stop_order in stop_orders:
                current_stop = Decimal(str(stop_order["slPrice"]))
                already_better = (
                    position.side == "LONG"
                    and current_stop >= target_stop
                ) or (
                    position.side == "SHORT"
                    and current_stop <= target_stop
                )
                if already_better:
                    protected_positions.add(position.position_id)
                    continue
                stop_quantity = (
                    stop_order.get("slQty") or stop_order.get("qty")
                )
                if not stop_quantity:
                    if len(stop_orders) == 1:
                        stop_quantity = position.quantity
                    else:
                        raise ValueError(
                            "Bitunix не вернул объём частичного SL"
                        )
                changes.append((position, stop_order, stop_quantity))
        for position, stop_order, stop_quantity in changes:
            await asyncio.to_thread(
                self.protections.modify_stop_loss,
                str(stop_order["id"]),
                str(target_stop),
                stop_quantity,
            )
            protected_positions.add(position.position_id)
        await asyncio.to_thread(
            self.journal.append,
            JournalEvent(
                event_type="trailing_stop",
                status="COMPLETED",
                symbol=positions[0].symbol,
                position_id=",".join(sorted(protected_positions)),
                stop_loss=str(target_stop),
                source_event_id=f"trailing-stop:{order_id}",
            ),
        )
        await self.notifier(
            f"🎯 TP{tp_number} исполнен\n\n"
            f"✅ SL оставшихся частей перенесён на TP{tp_number - 1}: "
            f"{target_stop}\n"
            f"Позиций защищено: {len(protected_positions)}"
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

        open_positions = await asyncio.to_thread(
            self.positions.get_open_positions,
        )
        positions = tuple(
            position
            for position in open_positions
            if position.position_id in proposal.position_ids
        )
        if not positions:
            raise ValueError("Все позиции сигнала уже закрыты")
        quote_precision = 8
        if self.market is not None:
            instrument = await asyncio.to_thread(
                self.market.get_trading_pair,
                positions[0].symbol,
            )
            quote_precision = instrument.quote_precision
        changes = []
        break_even_by_position = {}
        for position in positions:
            break_even = ProtectionService.fee_aware_break_even(
                position,
                quote_precision,
                self.taker_fee_rate,
            )
            break_even_by_position[position.position_id] = break_even
            protections = await asyncio.to_thread(
                self.protections.get_pending_tp_sl,
                position.symbol,
                position.position_id,
            )
            stop_orders = [
                item
                for item in protections
                if (
                    str(item.get("positionId", ""))
                    == position.position_id
                    and item.get("slPrice")
                    and item.get("id")
                )
            ]
            if not stop_orders:
                raise ValueError(
                    "Активный SL не найден для позиции "
                    f"{position.position_id}; перенос остановлен"
                )
            for stop_order in stop_orders:
                stop_quantity = (
                    stop_order.get("slQty")
                    or stop_order.get("qty")
                )
                if not stop_quantity:
                    if len(stop_orders) == 1:
                        stop_quantity = position.quantity
                    else:
                        raise ValueError(
                            "Bitunix не вернул объём частичного SL; "
                            "перенос остановлен"
                        )
                changes.append((
                    position,
                    stop_order,
                    stop_quantity,
                    break_even,
                ))
        for position, stop_order, stop_quantity, break_even in changes:
            await asyncio.to_thread(
                self.protections.modify_stop_loss,
                str(stop_order["id"]),
                str(break_even),
                stop_quantity,
            )
        await asyncio.to_thread(
            self.journal.append,
            JournalEvent(
                event_type="break_even",
                status="COMPLETED",
                symbol=positions[0].symbol,
                position_id=proposal.trigger_position_id,
                stop_loss=",".join(
                    str(break_even_by_position[position_id])
                    for position_id in proposal.position_ids
                    if position_id in break_even_by_position
                ),
                source_event_id=(
                    f"break-even:{proposal.trigger_position_id}"
                ),
            ),
        )
        self._tp1_order_positions = {
            order_id: position_id
            for order_id, position_id
            in self._tp1_order_positions.items()
            if position_id != proposal.trigger_position_id
        }
        return (
            "SL перенесён в fee-aware безубыток для позиций: "
            + ", ".join(
                f"{position_id} → {break_even}"
                for position_id, break_even
                in break_even_by_position.items()
            )
        )

    @staticmethod
    def _execution_client_prefix(client_id: str) -> str:
        if not client_id or "-" not in client_id:
            return ""
        return client_id.rsplit("-", 1)[0]

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
            position_visible = any(
                position.symbol == plan.symbol
                and position.side == plan.side.value
                for position in positions
            )
            if position_visible:
                await self._verify_plan_protections(
                    plan,
                    client_id,
                )
            pending_order = next(
                (
                    order
                    for order in orders
                    if order.client_id == client_id
                ),
                None,
            )
            if pending_order is not None:
                status = str(pending_order.status).rstrip("_")
            else:
                detail = await asyncio.to_thread(
                    self.orders.get_order_detail,
                    client_id,
                )
                status = str(detail.get("status", "")).rstrip("_")
            if status in {"CANCELED", "PART_FILLED_CANCELED"}:
                self.discard_plan(client_id)
            elif (
                status in {"INIT", "NEW", "PART_FILLED"}
                and client_id not in self._pending_plan_notifications
            ):
                self._pending_plan_notifications.add(client_id)
                await self.notifier(
                    "⏳ Контроль лимитного входа активен\n\n"
                    f"{plan.side.value} {plan.symbol}\n"
                    f"Цена входа: {plan.planned_entry_price}\n"
                    f"Запланировано TP: {len(plan.take_profits)}\n\n"
                    "TP и SL уже прикреплены на стороне Bitunix. "
                    "Бот ожидает исполнение только для уведомлений "
                    "и проверки защиты."
                )

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
                    entry_message = self._record_entry_fill(data)
                    if entry_message is not None:
                        return entry_message
                    client_id = str(data.get("clientId", ""))
                    if self._entry_group(client_id):
                        return None
                    side = OpenPosition.normalize_side(
                        data.get("side", "")
                    )
                    return (
                        f"✅ Ордер исполнен: {side} {symbol}\n"
                        f"Цена: {data.get('averagePrice', '')}; "
                        "объём: "
                        f"{data.get('dealAmount', data.get('qty', ''))}"
                    )
                return (
                    f"ℹ️ {description}: {symbol}, "
                    f"ID {data.get('orderId', '')}"
                )
        if channel == "position":
            event = str(data.get("event", ""))
            if event == "OPEN":
                expires_at = self._recent_bot_open_symbols.get(
                    symbol,
                    0,
                )
                tracked = any(
                    group.symbol == symbol
                    for group in self._entry_fill_groups.values()
                )
                if tracked or expires_at > time.monotonic():
                    return None
                return (
                    f"✅ Позиция открыта: "
                    f"{data.get('side', '')} {symbol}"
                )
            if event == "CLOSE":
                realized = str(data.get("realizedPNL", ""))
                fee = str(data.get("fee", ""))
                funding = str(data.get("funding", ""))
                net_pnl = self._net_pnl(realized, fee, funding)
                side = OpenPosition.normalize_side(
                    data.get("side", "")
                )
                net_text = (
                    f"{net_pnl} USDT"
                    if net_pnl is not None
                    else "нет данных"
                )
                fee_text = self._expense_text(fee)
                funding_text = (
                    f"{funding} USDT"
                    if funding
                    else "нет данных"
                )
                realized_text = (
                    f"{realized} USDT"
                    if realized
                    else "нет данных"
                )
                return (
                    f"🏁 Позиция закрыта: {side} {symbol}\n"
                    f"Реализованный PnL: {realized_text}\n"
                    f"Комиссия: {fee_text}\n"
                    f"Funding: {funding_text}\n"
                    f"Чистый результат: {net_text}"
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

    def _register_entry_notification(
        self,
        client_id: str,
        symbol: str,
    ) -> None:
        prefix = self._execution_client_prefix(client_id)
        if not prefix:
            return
        group = self._entry_fill_groups.setdefault(
            prefix,
            EntryFillGroup(set(), symbol=symbol),
        )
        group.expected_client_ids.add(client_id)

    def _entry_group(
        self,
        client_id: str,
    ) -> EntryFillGroup | None:
        prefix = self._execution_client_prefix(client_id)
        return self._entry_fill_groups.get(prefix)

    def _discard_entry_notification(self, client_id: str) -> None:
        prefix = self._execution_client_prefix(client_id)
        group = self._entry_fill_groups.get(prefix)
        if group is None:
            return
        group.expected_client_ids.discard(client_id)
        group.filled_client_ids.discard(client_id)
        group.verified_client_ids.discard(client_id)
        if not group.expected_client_ids:
            self._entry_fill_groups.pop(prefix, None)

    def _record_entry_fill(self, data: dict) -> str | None:
        client_id = str(data.get("clientId", ""))
        prefix = self._execution_client_prefix(client_id)
        group = self._entry_fill_groups.get(prefix)
        if group is None or client_id not in group.expected_client_ids:
            return None
        if client_id in group.filled_client_ids:
            return None
        try:
            quantity = Decimal(str(
                data.get("dealAmount", data.get("qty", ""))
            ))
            average_price = Decimal(str(data.get("averagePrice", "")))
            fee = abs(Decimal(str(data.get("fee", "0") or "0")))
        except InvalidOperation:
            return (
                "⚠️ Bitunix подтвердил часть входа, но вернул "
                "некорректные цену, объём или комиссию. "
                f"ID: {data.get('orderId', '')}"
            )
        group.symbol = str(data.get("symbol", group.symbol))
        group.side = OpenPosition.normalize_side(data.get("side", ""))
        group.quantity += quantity
        group.notional += quantity * average_price
        group.fee += fee
        group.filled_client_ids.add(client_id)
        self._recent_bot_open_symbols[group.symbol] = (
            time.monotonic() + 60
        )
        if group.filled_client_ids != group.expected_client_ids:
            return None
        weighted_price = (
            group.notional / group.quantity
            if group.quantity
            else Decimal("0")
        )
        message = (
            f"✅ {group.side} {group.symbol} открыт\n\n"
            f"Средняя цена: {self._decimal_text(weighted_price)}\n"
            f"Общий объём: {self._decimal_text(group.quantity)}\n"
            "Комиссия входа: "
            f"{self._decimal_text(group.fee)} USDT"
        )
        if group.verified_client_ids == group.expected_client_ids:
            self._entry_fill_groups.pop(prefix, None)
        return message

    def _mark_protection_verified(
        self,
        client_id: str,
    ) -> str | None:
        prefix = self._execution_client_prefix(client_id)
        group = self._entry_fill_groups.get(prefix)
        if group is None or client_id not in group.expected_client_ids:
            return None
        group.verified_client_ids.add(client_id)
        if group.verified_client_ids != group.expected_client_ids:
            return None
        tp_numbers = sorted(
            self._tp_number_from_client_id(item)
            for item in group.expected_client_ids
        )
        tp_range = (
            f"TP{tp_numbers[0]}"
            if len(tp_numbers) == 1
            else f"TP{tp_numbers[0]}–TP{tp_numbers[-1]}"
        )
        activity = "активен" if len(tp_numbers) == 1 else "активны"
        message = (
            f"✅ Защита {group.symbol} проверена: "
            f"{tp_range} {activity}; SL задан во входных ордерах"
        )
        if group.filled_client_ids == group.expected_client_ids:
            self._entry_fill_groups.pop(prefix, None)
        return message

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
        calculated_net_pnl = PositionMonitor._net_pnl(
            realized,
            fee,
            funding,
        )
        net_pnl = (
            str(calculated_net_pnl)
            if calculated_net_pnl is not None
            else ""
        )
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

    @staticmethod
    def _net_pnl(
        realized: str,
        fee: str,
        funding: str,
    ) -> Decimal | None:
        if not realized or not fee or not funding:
            return None
        try:
            return (
                Decimal(realized)
                + Decimal(funding)
                - abs(Decimal(fee))
            )
        except InvalidOperation:
            return None

    @staticmethod
    def _expense_text(value: str) -> str:
        if not value:
            return "нет данных"
        try:
            expense = abs(Decimal(value))
        except InvalidOperation:
            return "нет данных"
        if expense == 0:
            return "0 USDT"
        return f"-{expense} USDT"

    @staticmethod
    def _decimal_text(value: Decimal) -> str:
        text = format(value, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text or "0"

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
