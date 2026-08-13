import asyncio
import json
import time
from dataclasses import asdict, dataclass, replace
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from pathlib import Path
from threading import Lock
from typing import Awaitable, Callable

from models.signal import OrderSide
from models.signal import TradeSignal
from models.trade import PlannedTakeProfit, TradePlan
from config.settings import TradeSettings
from services.trade_planner import TradePlanner


@dataclass(frozen=True)
class EmulatedTrigger:
    plan: TradePlan
    status: str
    created_at: float
    updated_at: float
    last_price: float | None = None
    order_ids: tuple[str, ...] = ()
    limit_placed_at: float | None = None

    def condition_met(self, price: float) -> bool:
        trigger = self.plan.trigger_price
        if trigger is None:
            return False
        if self.plan.side is OrderSide.LONG:
            return price >= trigger
        return price <= trigger


class EmulatedTriggerStore:
    """Small atomic JSON store; it contains no credentials."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = Lock()

    def load(self) -> dict[str, EmulatedTrigger]:
        with self._lock:
            if not self.path.exists():
                return {}
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        return {
            item["plan"]["execution_id"]: self._decode(item)
            for item in payload
        }

    def save(self, records: dict[str, EmulatedTrigger]) -> None:
        payload = [self._encode(item) for item in records.values()]
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.path)

    @staticmethod
    def _encode(record: EmulatedTrigger) -> dict:
        plan = asdict(record.plan)
        plan["side"] = record.plan.side.value
        return {
            "plan": plan,
            "status": record.status,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "last_price": record.last_price,
            "order_ids": record.order_ids,
            "limit_placed_at": record.limit_placed_at,
        }

    @staticmethod
    def _decode(payload: dict) -> EmulatedTrigger:
        raw = dict(payload["plan"])
        raw["side"] = OrderSide(raw["side"])
        raw["take_profits"] = tuple(
            PlannedTakeProfit(**item) for item in raw["take_profits"]
        )
        raw["raw_take_profits"] = tuple(
            raw.get("raw_take_profits", ())
        )
        return EmulatedTrigger(
            plan=TradePlan(**raw),
            status=payload["status"],
            created_at=float(payload["created_at"]),
            updated_at=float(payload["updated_at"]),
            last_price=payload.get("last_price"),
            order_ids=tuple(payload.get("order_ids", ())),
            limit_placed_at=(
                float(payload["limit_placed_at"])
                if payload.get("limit_placed_at") is not None
                else None
            ),
        )


Notify = Callable[[str], Awaitable[None]]
Register = Callable[[TradePlan, object], Awaitable[None]]


class EmulatedTriggerService:
    ACTIVE = "ARMED"
    SUSPENDED = "SUSPENDED"

    def __init__(
        self,
        market_service,
        execution_service,
        account_service,
        order_service,
        position_service,
        store: EmulatedTriggerStore,
        notify: Notify,
        register_execution: Register,
        *,
        poll_interval: float = 3.0,
        max_age_seconds: float = 86400,
        limit_offset_ticks: int = 2,
        take_profit_offset_ticks: int = 2,
        price_retry_count: int = 3,
        price_retry_delay: float = 3,
        max_entry_deviation_percent: Decimal = Decimal("0.15"),
        limit_timeout_seconds: float = 30,
    ):
        self.market = market_service
        self.execution = execution_service
        self.account = account_service
        self.orders = order_service
        self.positions = position_service
        self.store = store
        self.notify = notify
        self.register_execution = register_execution
        self.poll_interval = poll_interval
        self.max_age_seconds = max_age_seconds
        self.limit_offset_ticks = limit_offset_ticks
        self.take_profit_offset_ticks = take_profit_offset_ticks
        self.price_retry_count = price_retry_count
        self.price_retry_delay = price_retry_delay
        self.max_entry_deviation_percent = Decimal(
            max_entry_deviation_percent
        )
        self.limit_timeout_seconds = limit_timeout_seconds
        self.records = store.load()
        self._task = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        self._limit_tasks: dict[str, asyncio.Task] = {}

    async def start(self) -> tuple[EmulatedTrigger, ...]:
        # An ARMED record came from an earlier process. The price may have
        # crossed while it was offline, so late entry is deliberately blocked.
        now = time.time()
        for execution_id, record in tuple(self.records.items()):
            if record.status == self.ACTIVE:
                self.records[execution_id] = replace(
                    record,
                    status=self.SUSPENDED,
                    updated_at=now,
                )
        await asyncio.to_thread(self.store.save, self.records)
        for execution_id, record in self.records.items():
            if record.status in {"LIMIT_PLACED", "PARTIAL_PLACED"}:
                self._schedule_limit_timeout(execution_id)
        self._task = asyncio.create_task(self._run(), name="entry-triggers")
        return self.suspended()

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        for task in self._limit_tasks.values():
            task.cancel()
        if self._limit_tasks:
            await asyncio.gather(
                *self._limit_tasks.values(),
                return_exceptions=True,
            )
        self._limit_tasks.clear()
        now = time.time()
        for execution_id, record in tuple(self.records.items()):
            if record.status == self.ACTIVE:
                self.records[execution_id] = replace(
                    record,
                    status=self.SUSPENDED,
                    updated_at=now,
                )
        await asyncio.to_thread(self.store.save, self.records)

    async def arm(self, plan: TradePlan) -> EmulatedTrigger:
        if not plan.is_emulated_trigger:
            raise ValueError("План не содержит локального триггера")
        probe = EmulatedTrigger(plan, self.ACTIVE, time.time(), time.time())
        current_price = await self.price(probe)
        if current_price is None:
            raise ValueError(
                "Не удалось проверить текущую цену Bitunix; "
                "триггер не активирован"
            )
        if probe.condition_met(current_price):
            raise ValueError(
                "Цена уже достигла триггера до подтверждения; "
                "пересчитайте вход по текущей цене"
            )
        now = time.time()
        record = EmulatedTrigger(
            plan, self.ACTIVE, now, now, current_price
        )
        async with self._lock:
            self.records[plan.execution_id] = record
            await asyncio.to_thread(self.store.save, self.records)
        return record

    def suspended(self) -> tuple[EmulatedTrigger, ...]:
        return tuple(
            item for item in self.records.values()
            if item.status == self.SUSPENDED
        )

    def get(self, execution_id: str) -> EmulatedTrigger | None:
        return self.records.get(execution_id)

    async def price(self, record: EmulatedTrigger) -> float | None:
        try:
            ticker = await asyncio.to_thread(
                self.market.get_ticker, record.plan.symbol
            )
            return ticker.last_price if ticker else None
        except Exception:
            return None

    async def price_with_retries(
        self,
        record: EmulatedTrigger,
    ) -> float | None:
        for attempt in range(self.price_retry_count):
            price = await self.price(record)
            if price is not None:
                return price
            if attempt + 1 < self.price_retry_count:
                await asyncio.sleep(self.price_retry_delay)
        return None

    async def rearm(self, execution_id: str) -> tuple[bool, str, float | None]:
        record = self.records.get(execution_id)
        if record is None or record.status != self.SUSPENDED:
            return False, "Триггер уже изменён или не найден", None
        price = await self.price_with_retries(record)
        if price is None:
            return False, "Не удалось получить текущую цену Bitunix", None
        if time.time() - record.created_at > self.max_age_seconds:
            return False, "Срок действия триггера истёк", price
        if record.condition_met(price):
            return False, "Цена уже пересекла триггер во время остановки", price
        updated = replace(
            record,
            status=self.ACTIVE,
            updated_at=time.time(),
            last_price=price,
        )
        async with self._lock:
            self.records[execution_id] = updated
            await asyncio.to_thread(self.store.save, self.records)
        return True, "Триггер снова активен", price

    async def cancel(self, execution_id: str) -> bool:
        record = self.records.get(execution_id)
        if record is None or record.status not in {self.ACTIVE, self.SUSPENDED}:
            return False
        async with self._lock:
            self.records[execution_id] = replace(
                record, status="CANCELLED", updated_at=time.time()
            )
            await asyncio.to_thread(self.store.save, self.records)
        return True

    async def _run(self) -> None:
        while not self._stop.is_set():
            active = [
                item for item in self.records.values()
                if item.status == self.ACTIVE
            ]
            for record in active:
                await self._check(record)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.poll_interval
                )
            except TimeoutError:
                pass

    async def _check(self, record: EmulatedTrigger) -> None:
        if time.time() - record.created_at > self.max_age_seconds:
            await self._finish(
                record,
                "EXPIRED",
                record.last_price or record.plan.current_price,
                f"⌛ Триггер {record.plan.side.value} "
                f"{record.plan.symbol} истёк; ордер не отправлен.",
            )
            return
        price = await self.price_with_retries(record)
        if price is None:
            await self._finish(
                record,
                self.SUSPENDED,
                record.last_price or record.plan.current_price,
                f"⚠️ Триггер {record.plan.side.value} "
                f"{record.plan.symbol} приостановлен: цена Bitunix "
                "недоступна. Автоматического позднего входа не будет. "
                "После восстановления связи перезапустите бот и "
                "проверьте текущую цену перед повторной активацией.",
            )
            return
        current = self.records.get(record.plan.execution_id)
        if current is None or current.status != self.ACTIVE:
            return
        self.records[record.plan.execution_id] = replace(
            current, last_price=price, updated_at=time.time()
        )
        if not record.condition_met(price):
            return
        trigger_price = Decimal(str(record.plan.trigger_price))
        deviation = (
            abs(Decimal(str(price)) - trigger_price)
            / trigger_price
            * Decimal("100")
        )
        if deviation > self.max_entry_deviation_percent:
            await self._finish(
                record,
                self.SUSPENDED,
                price,
                f"⚠️ Триггер {record.plan.side.value} "
                f"{record.plan.symbol} достигнут, но цена ушла на "
                f"{deviation:.3f}% от уровня. Вход не отправлен; "
                "пересчитайте сделку по текущей цене.",
            )
            return
        async with self._lock:
            current = self.records.get(record.plan.execution_id)
            if current is None or current.status != self.ACTIVE:
                return
            self.records[record.plan.execution_id] = replace(
                current, status="TRIGGERING", last_price=price,
                updated_at=time.time(),
            )
            await asyncio.to_thread(self.store.save, self.records)
        await self._execute(current, price)

    async def _execute(self, record: EmulatedTrigger, price: float) -> None:
        plan = record.plan
        try:
            orders, positions = await asyncio.gather(
                asyncio.to_thread(self.orders.get_pending_orders, plan.symbol),
                asyncio.to_thread(self.positions.get_open_positions, plan.symbol),
            )
            if orders or positions:
                await self._finish(
                    record, "SUSPENDED", price,
                    f"⚠️ Триггер {plan.side.value} {plan.symbol} достигнут, "
                    "но вход заблокирован: уже есть ордер или позиция по символу.",
                )
                return
            limit_plan = await asyncio.to_thread(
                self._build_limit_plan,
                plan,
                price,
            )
            result = await asyncio.to_thread(
                self.execution.execute,
                limit_plan,
            )
            if result.orders:
                await self.register_execution(limit_plan, result)
            status = "LIMIT_PLACED" if result.success else (
                "PARTIAL_PLACED" if result.orders else "FAILED"
            )
            text = (
                f"✅ Сработал триггер {plan.side.value} {plan.symbol}\n"
                f"Триггер: {plan.trigger_price}; цена Bitunix: {price}\n"
                f"Отправлен LIMIT: {limit_plan.limit_price} "
                f"({self.limit_offset_ticks} тик.)\n"
                f"Статус входа: {status}; ожидает исполнения"
            )
            if result.error:
                text += f"\nОшибка: {result.error}"
            placed_at = time.time()
            await self._finish(
                record,
                status,
                price,
                text,
                order_ids=tuple(item.order_id for item in result.orders),
                limit_placed_at=placed_at,
            )
            if result.orders:
                self._schedule_limit_timeout(plan.execution_id)
        except Exception as error:
            await self._finish(
                record,
                "UNKNOWN",
                price,
                f"⚠️ Ошибка после срабатывания {plan.symbol}: "
                f"{type(error).__name__}. Повторный вход не выполнялся; "
                "проверьте Bitunix.",
            )

    def _build_limit_plan(
        self,
        plan: TradePlan,
        reference_price: float,
    ) -> TradePlan:
        instrument = self.market.get_trading_pair(plan.symbol)
        tick = Decimal("1").scaleb(-instrument.quote_precision)
        offset = tick * Decimal(self.limit_offset_ticks)
        raw_price = Decimal(str(reference_price))
        if plan.side is OrderSide.LONG:
            limit_price = (raw_price + offset).quantize(
                tick,
                rounding=ROUND_UP,
            )
        else:
            limit_price = (raw_price - offset).quantize(
                tick,
                rounding=ROUND_DOWN,
            )
        if limit_price <= 0:
            raise ValueError(
                "Рассчитанная LIMIT-цена должна быть положительной"
            )

        raw_take_profits = tuple(
            plan.raw_take_profits
            or tuple(item.price for item in plan.take_profits)
        )
        signal = TradeSignal(
            symbol=plan.symbol,
            side=plan.side,
            entry_min=plan.entry_min,
            entry_max=plan.entry_max,
            take_profits=list(raw_take_profits),
            stop_loss=(
                plan.signal_stop_loss
                if plan.signal_stop_loss is not None
                else plan.stop_loss
            ),
        )
        account = self.account.get_account("USDT")
        planner = TradePlanner(
            self.market,
            TradeSettings(
                leverage=plan.leverage,
                risk_percent=plan.risk_percent,
                max_tp_count=len(plan.take_profits),
                tp_offset_ticks=self.take_profit_offset_ticks,
                enable_emulated_triggers=False,
                max_stop_roi_percent=plan.max_stop_roi_percent,
                taker_fee_rate=plan.taker_fee_rate,
            ),
        )
        replanned = planner.create_plan(
            signal,
            account,
            limit_price_override=float(limit_price),
        )
        return replace(replanned, execution_id=plan.execution_id)

    async def _finish(
        self, record: EmulatedTrigger, status: str,
        price: float, message: str,
        *,
        order_ids: tuple[str, ...] | None = None,
        limit_placed_at: float | None = None,
    ) -> None:
        async with self._lock:
            changes = {
                "status": status,
                "last_price": price,
                "updated_at": time.time(),
            }
            if order_ids is not None:
                changes["order_ids"] = order_ids
            if limit_placed_at is not None:
                changes["limit_placed_at"] = limit_placed_at
            self.records[record.plan.execution_id] = replace(
                record,
                **changes,
            )
            await asyncio.to_thread(self.store.save, self.records)
        await self.notify(message)

    def _schedule_limit_timeout(self, execution_id: str) -> None:
        existing = self._limit_tasks.get(execution_id)
        if existing is not None and not existing.done():
            return
        self._limit_tasks[execution_id] = asyncio.create_task(
            self._cancel_stale_limit(execution_id),
            name=f"trigger-limit-timeout-{execution_id}",
        )

    async def _cancel_stale_limit(self, execution_id: str) -> None:
        try:
            record = self.records.get(execution_id)
            if record is None or not record.order_ids:
                return
            placed_at = record.limit_placed_at or record.updated_at
            remaining = max(
                0,
                self.limit_timeout_seconds - (time.time() - placed_at),
            )
            if remaining:
                await asyncio.sleep(remaining)
            pending = await asyncio.to_thread(
                self.orders.get_pending_orders,
                record.plan.symbol,
            )
            tracked = set(record.order_ids)
            pending_ids = tuple(
                item.order_id for item in pending
                if item.order_id in tracked
            )
            if not pending_ids:
                return
            result = await asyncio.to_thread(
                self.orders.cancel_orders,
                record.plan.symbol,
                pending_ids,
            )
            failed = len(result.failed)
            if failed:
                message = (
                    "⚠️ Истёк срок LIMIT после триггера. "
                    f"Не удалось отменить {failed} из {len(pending_ids)} "
                    "остатков; проверьте Bitunix."
                )
                status = "LIMIT_CANCEL_PARTIAL"
            else:
                message = (
                    "⌛ LIMIT после триггера не исполнился полностью за "
                    f"{self.limit_timeout_seconds:g} сек. "
                    f"Неисполненных остатков отменено: {len(pending_ids)}. "
                    "Исполненная часть, если она есть, остаётся с TP и SL."
                )
                status = "LIMIT_TIMEOUT"
            await self._set_timeout_status(execution_id, status, message)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._set_timeout_status(
                execution_id,
                "LIMIT_CANCEL_FAILED",
                "⚠️ Не удалось проверить или отменить просроченный LIMIT "
                f"после триггера: {type(error).__name__}. "
                "Проверьте ордер на Bitunix вручную.",
            )
        finally:
            self._limit_tasks.pop(execution_id, None)

    async def _set_timeout_status(
        self,
        execution_id: str,
        status: str,
        message: str,
    ) -> None:
        async with self._lock:
            current = self.records.get(execution_id)
            if current is not None:
                self.records[execution_id] = replace(
                    current,
                    status=status,
                    updated_at=time.time(),
                )
                await asyncio.to_thread(self.store.save, self.records)
        await self.notify(message)
