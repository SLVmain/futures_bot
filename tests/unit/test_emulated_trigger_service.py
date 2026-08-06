import asyncio
from types import SimpleNamespace

import pytest

from core.api_models import AccountBalance, MarketTicker, TradingPair
from models.signal import OrderSide
from models.trade import (
    PlacedOrder,
    PlannedTakeProfit,
    TradeExecutionResult,
    TradePlan,
)
from services.emulated_trigger_service import (
    EmulatedTriggerService,
    EmulatedTriggerStore,
)


@pytest.fixture(autouse=True)
def run_blocking_calls_inline(monkeypatch):
    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(
        "services.emulated_trigger_service.asyncio.to_thread",
        inline,
    )


def make_plan(side=OrderSide.SHORT):
    take_profit = 45 if side is OrderSide.SHORT else 55
    return TradePlan(
        symbol="BTCUSDT",
        side=side,
        entry_min=49,
        entry_max=51,
        current_price=60 if side is OrderSide.SHORT else 40,
        in_range=False,
        total_quantity=2,
        stop_loss=55 if side is OrderSide.SHORT else 45,
        take_profits=(PlannedTakeProfit(take_profit, 2),),
        leverage=10,
        risk_percent=1,
        risk_budget=10,
        trigger_price=50,
        raw_take_profits=(take_profit,),
        execution_id="trigger-one",
    )


class Market:
    def __init__(self, price):
        self.price = price

    def get_ticker(self, symbol):
        return MarketTicker(symbol, self.price)

    def get_trading_pair(self, symbol):
        return TradingPair(
            symbol=symbol,
            min_trade_volume="0.001",
            max_market_order_volume="100000",
            base_precision=3,
            quote_precision=2,
            min_leverage=1,
            max_leverage=125,
            symbol_status="OPEN",
            api_supported=True,
        )


class MissingMarket:
    def get_ticker(self, symbol):
        return None


class SequenceMarket(Market):
    def __init__(self, outcomes):
        super().__init__(60)
        self.outcomes = iter(outcomes)
        self.ticker_calls = 0

    def get_ticker(self, symbol):
        self.ticker_calls += 1
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return MarketTicker(symbol, outcome) if outcome is not None else None


class EmptyOrders:
    def get_pending_orders(self, symbol):
        return ()


class TrackedOrders(EmptyOrders):
    def __init__(self):
        self.pending = ()
        self.cancelled = []

    def get_pending_orders(self, symbol):
        return self.pending

    def cancel_orders(self, symbol, order_ids):
        self.cancelled.append((symbol, order_ids))
        self.pending = ()
        return SimpleNamespace(failed=())


class EmptyPositions:
    def get_open_positions(self, symbol):
        return ()


class Account:
    def get_account(self, margin_coin):
        return AccountBalance(margin_coin, "1000")


class Executor:
    def __init__(self):
        self.plans = []

    def execute(self, plan):
        self.plans.append(plan)
        return TradeExecutionResult(
            True,
            orders=(PlacedOrder(1, 45, 2, "order-1", True),),
            stop_loss=plan.stop_loss,
            simulated=True,
        )


def make_service(tmp_path, market, executor=None, orders=None, **settings):
    messages = []
    registered = []

    async def notify(text):
        messages.append(text)

    async def register(plan, result):
        registered.append((plan, result))

    service = EmulatedTriggerService(
        market,
        executor or Executor(),
        Account(),
        orders or EmptyOrders(),
        EmptyPositions(),
        EmulatedTriggerStore(tmp_path / "triggers.json"),
        notify,
        register,
        poll_interval=3600,
        price_retry_delay=0,
        **settings,
    )
    return service, messages, registered


def test_trigger_is_persisted_and_suspended_after_restart(tmp_path):
    async def scenario():
        service, _, _ = make_service(tmp_path, Market(60))
        await service.arm(make_plan())
        await service.stop()

        restored, _, _ = make_service(tmp_path, Market(60))
        suspended = await restored.start()
        try:
            assert len(suspended) == 1
            assert suspended[0].status == "SUSPENDED"
            assert suspended[0].last_price == 60
        finally:
            await restored.stop()

    asyncio.run(scenario())


def test_rearm_is_blocked_if_price_crossed_while_offline(tmp_path):
    async def scenario():
        service, _, _ = make_service(tmp_path, Market(60))
        await service.arm(make_plan())
        await service.stop()
        restored, _, _ = make_service(tmp_path, Market(49))
        await restored.start()
        try:
            ok, message, price = await restored.rearm("trigger-one")
            assert ok is False
            assert "пересекла" in message
            assert price == 49
        finally:
            await restored.stop()

    asyncio.run(scenario())


def test_trigger_executes_aggressive_limit_plan_only_once(tmp_path):
    async def scenario():
        executor = Executor()
        service, messages, registered = make_service(
            tmp_path, Market(51), executor
        )
        await service.arm(make_plan())
        service.market.price = 49.95
        record = service.get("trigger-one")
        await asyncio.gather(service._check(record), service._check(record))

        assert len(executor.plans) == 1
        assert executor.plans[0].order_type == "LIMIT"
        assert executor.plans[0].limit_price == 49.93
        assert executor.plans[0].trigger_price is None
        assert service.get("trigger-one").status == "LIMIT_PLACED"
        assert len(registered) == 1
        assert "Сработал триггер" in messages[0]

    asyncio.run(scenario())


def test_long_trigger_limit_is_above_reference_price(tmp_path):
    service, _, _ = make_service(tmp_path, Market(51))

    limit_plan = service._build_limit_plan(
        make_plan(OrderSide.LONG),
        51,
    )

    assert limit_plan.order_type == "LIMIT"
    assert limit_plan.limit_price == 51.02
    assert limit_plan.total_quantity < make_plan(
        OrderSide.LONG
    ).total_quantity


def test_arm_refuses_stale_confirmation_after_crossing(tmp_path):
    async def scenario():
        service, _, _ = make_service(tmp_path, Market(49))
        try:
            await service.arm(make_plan())
        except ValueError as error:
            assert "уже достигла" in str(error)
        else:
            raise AssertionError("crossed trigger was armed")

    asyncio.run(scenario())


def test_active_trigger_suspends_when_price_becomes_unavailable(tmp_path):
    async def scenario():
        service, messages, _ = make_service(tmp_path, Market(60))
        await service.arm(make_plan())
        service.market = MissingMarket()
        await service._check(service.get("trigger-one"))

        assert service.get("trigger-one").status == "SUSPENDED"
        assert "позднего входа не будет" in messages[0]

    asyncio.run(scenario())


def test_price_recovers_during_retries_without_warning(tmp_path):
    async def scenario():
        executor = Executor()
        service, messages, _ = make_service(
            tmp_path,
            Market(60),
            executor,
            price_retry_count=3,
        )
        await service.arm(make_plan())
        service.market = SequenceMarket(
            (None, ConnectionError(), 49.95, 49.95)
        )

        await service._check(service.get("trigger-one"))

        # Three checks belong to retry handling; the planner then reads the
        # ticker once more while rebuilding risk for the resulting LIMIT.
        assert service.market.ticker_calls == 4
        assert len(executor.plans) == 1
        assert not any("приостановлен" in item for item in messages)

    asyncio.run(scenario())


def test_trigger_blocks_entry_after_excessive_price_jump(tmp_path):
    async def scenario():
        executor = Executor()
        service, messages, _ = make_service(
            tmp_path, Market(60), executor
        )
        await service.arm(make_plan())
        service.market.price = 49

        await service._check(service.get("trigger-one"))

        assert executor.plans == []
        assert service.get("trigger-one").status == "SUSPENDED"
        assert "цена ушла" in messages[0]

    asyncio.run(scenario())


def test_stale_trigger_limit_is_cancelled_by_exact_order_id(tmp_path):
    async def scenario():
        orders = TrackedOrders()
        service, messages, _ = make_service(
            tmp_path,
            Market(60),
            orders=orders,
            limit_timeout_seconds=0.01,
        )
        await service.arm(make_plan())
        service.market.price = 49.95
        await service._check(service.get("trigger-one"))
        orders.pending = (SimpleNamespace(order_id="order-1"),)
        await asyncio.sleep(0.02)

        assert orders.cancelled == [("BTCUSDT", ("order-1",))]
        assert service.get("trigger-one").status == "LIMIT_TIMEOUT"
        assert "отменено: 1" in messages[-1]

    asyncio.run(scenario())


def test_placed_order_identity_survives_store_reload(tmp_path):
    async def scenario():
        service, _, _ = make_service(tmp_path, Market(60))
        await service.arm(make_plan())
        service.market.price = 49.95
        await service._check(service.get("trigger-one"))

        restored, _, _ = make_service(tmp_path, Market(60))
        record = restored.get("trigger-one")
        assert record.order_ids == ("order-1",)
        assert record.limit_placed_at is not None

    asyncio.run(scenario())
