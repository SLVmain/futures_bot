import asyncio
import json
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from threading import Lock
from typing import Awaitable, Callable

from models.signal import OrderSide
from models.trade import PlannedTakeProfit, TradePlan


@dataclass(frozen=True)
class EmulatedTrigger:
    plan: TradePlan
    status: str
    created_at: float
    updated_at: float
    last_price: float | None = None

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
        order_service,
        position_service,
        store: EmulatedTriggerStore,
        notify: Notify,
        register_execution: Register,
        *,
        poll_interval: float = 3.0,
        max_age_seconds: float = 86400,
    ):
        self.market = market_service
        self.execution = execution_service
        self.orders = order_service
        self.positions = position_service
        self.store = store
        self.notify = notify
        self.register_execution = register_execution
        self.poll_interval = poll_interval
        self.max_age_seconds = max_age_seconds
        self.records = store.load()
        self._task = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()

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
        self._task = asyncio.create_task(self._run(), name="entry-triggers")
        return self.suspended()

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
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

    async def rearm(self, execution_id: str) -> tuple[bool, str, float | None]:
        record = self.records.get(execution_id)
        if record is None or record.status != self.SUSPENDED:
            return False, "Триггер уже изменён или не найден", None
        price = await self.price(record)
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
        price = await self.price(record)
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
            market_plan = replace(
                plan,
                current_price=price,
                in_range=True,
                limit_price=None,
                trigger_price=None,
            )
            result = await asyncio.to_thread(self.execution.execute, market_plan)
            if result.orders:
                await self.register_execution(market_plan, result)
            status = "EXECUTED" if result.success else (
                "PARTIAL" if result.orders else "FAILED"
            )
            text = (
                f"✅ Сработал триггер {plan.side.value} {plan.symbol}\n"
                f"Триггер: {plan.trigger_price}; цена Bitunix: {price}\n"
                f"Статус входа: {status}"
            )
            if result.error:
                text += f"\nОшибка: {result.error}"
            await self._finish(record, status, price, text)
        except Exception as error:
            await self._finish(
                record,
                "UNKNOWN",
                price,
                f"⚠️ Ошибка после срабатывания {plan.symbol}: "
                f"{type(error).__name__}. Повторный вход не выполнялся; "
                "проверьте Bitunix.",
            )

    async def _finish(
        self, record: EmulatedTrigger, status: str,
        price: float, message: str,
    ) -> None:
        async with self._lock:
            self.records[record.plan.execution_id] = replace(
                record, status=status, last_price=price,
                updated_at=time.time(),
            )
            await asyncio.to_thread(self.store.save, self.records)
        await self.notify(message)
