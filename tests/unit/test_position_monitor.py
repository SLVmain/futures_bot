import asyncio
import csv
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from models.signal import OrderSide
from models.trade import PlannedTakeProfit, TradePlan
from services.position_monitor import PositionMonitor
from services.trade_journal import CsvTradeJournal
from tests.unit.test_protection_service import make_position


@pytest.fixture
def run_blocking_calls_inline(monkeypatch):
    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(
        "services.position_monitor.asyncio.to_thread",
        inline,
    )


class FakeOrders:
    def get_pending_orders(self):
        return ()


class FakePositions:
    def get_open_positions(self, *args):
        return (make_position(),)


class DelayedFakePositions:
    def __init__(self, empty_responses):
        self.empty_responses = empty_responses
        self.calls = 0
        self.position = make_position()

    def get_open_positions(self, *args):
        self.calls += 1
        if self.calls <= self.empty_responses:
            return ()
        return (self.position,)


class FakeProtections:
    def __init__(self):
        self.placed = []
        self.modified = []
        self.pending = [{
            "id": "tp-existing",
            "positionId": "position-1",
            "tpPrice": "55",
            "tpQty": "1",
            "slPrice": "",
        }]

    def get_pending_tp_sl(self, *args):
        return tuple(self.pending)

    def unprotected_positions(self, positions, protections):
        return ((positions[0], ("SL",)),)

    def place_take_profit(
        self,
        symbol,
        position_id,
        price,
        quantity,
    ):
        self.placed.append(
            (symbol, position_id, price, quantity)
        )
        return f"tp-{len(self.placed)}"

    def modify_stop_loss(
        self,
        order_id,
        price,
        quantity,
    ):
        self.modified.append((order_id, price, quantity))
        return order_id


def make_monitor(tmp_path, notifications, **kwargs):
    async def notify(message, action_id=None):
        notifications.append(message)

    return PositionMonitor(
        FakeOrders(),
        FakePositions(),
        FakeProtections(),
        CsvTradeJournal(tmp_path / "journal.csv"),
        notify,
        **kwargs,
    )


def make_single_tp_plan(quantity=1):
    return TradePlan(
        symbol="BTCUSDT",
        side=OrderSide.LONG,
        entry_min=49,
        entry_max=51,
        current_price=50,
        in_range=True,
        total_quantity=quantity,
        stop_loss=45,
        take_profits=(PlannedTakeProfit(55, quantity),),
        leverage=10,
        risk_percent=1,
        risk_budget=10,
    )


def test_order_event_is_journaled_notified_and_deduplicated(tmp_path):
    async def scenario():
        notifications = []
        monitor = make_monitor(tmp_path, notifications)
        event = {
            "ch": "order",
            "ts": 100,
            "data": {
                "orderId": "order-1",
                "symbol": "BTCUSDT",
                "orderStatus": "FILLED",
                "side": "BUY",
                "type": "MARKET",
                "averagePrice": "50.1",
                "dealAmount": "1",
                "fee": "0.03",
            },
        }

        await monitor.handle_event(event)
        await monitor.handle_event(event)

        assert len(notifications) == 1
        assert "Ордер исполнен: LONG BTCUSDT" in notifications[0]
        assert "Цена: 50.1; объём: 1" in notifications[0]

    asyncio.run(scenario())


def test_split_entry_fills_are_combined_into_one_notification(tmp_path):
    async def scenario():
        notifications = []
        monitor = make_monitor(tmp_path, notifications)

        async def skip_protection_check(data):
            return None

        monitor._verify_attached_protections = skip_protection_check
        for index in range(1, 4):
            monitor.register_plan(
                f"bot-execution-abc-{index}",
                make_single_tp_plan(),
            )

        for index, price in enumerate(("50", "50.1", "49.9"), 1):
            await monitor.handle_event({
                "ch": "order",
                "ts": 100 + index,
                "data": {
                    "orderId": f"order-{index}",
                    "clientId": f"bot-execution-abc-{index}",
                    "symbol": "BTCUSDT",
                    "orderStatus": "FILLED",
                    "side": "BUY",
                    "averagePrice": price,
                    "dealAmount": "1",
                    "fee": "0.01",
                },
            })

        assert notifications == [
            "✅ LONG BTCUSDT открыт\n\n"
            "Средняя цена: 50\n"
            "Общий объём: 3\n"
            "Комиссия входа: 0.03 USDT"
        ]

    asyncio.run(scenario())


def test_split_entry_protection_checks_are_combined(tmp_path):
    notifications = []
    monitor = make_monitor(tmp_path, notifications)
    for index in range(1, 4):
        monitor.register_plan(
            f"bot-execution-abc-{index}",
            make_single_tp_plan(),
        )

    assert monitor._mark_protection_verified(
        "bot-execution-abc-1"
    ) is None
    assert monitor._mark_protection_verified(
        "bot-execution-abc-2"
    ) is None
    assert monitor._mark_protection_verified(
        "bot-execution-abc-3"
    ) == (
        "✅ Защита BTCUSDT проверена: TP1–TP3 активны; "
        "SL задан во входных ордерах"
    )


def test_bot_position_open_event_is_suppressed(tmp_path):
    async def scenario():
        monitor = make_monitor(tmp_path, [])
        monitor.register_plan("bot-execution-abc-1", make_single_tp_plan())

        message = await monitor._notification(
            "position",
            {
                "event": "OPEN",
                "symbol": "BTCUSDT",
                "side": "BUY",
            },
            "OPEN",
        )

        assert message is None

    asyncio.run(scenario())


def test_take_profit_fill_notification_contains_position_data(
    tmp_path,
):
    async def scenario():
        notifications = []
        monitor = make_monitor(tmp_path, notifications)
        monitor._tp_orders["tp-2"] = ("position-1", 2)

        await monitor.handle_event({
            "ch": "tpsl",
            "ts": 200,
            "data": {
                "orderId": "tp-2",
                "positionId": "position-1",
                "symbol": "BTCUSDT",
                "side": "SELL",
                "status": "FILLED",
                "tpPrice": "45",
                "tpQty": "0.3",
            },
        })

        message = notifications[0]
        assert "Исполнен TP2" in message
        assert "Позиция: SHORT BTCUSDT" in message
        assert "Триггер: 45" in message
        assert "Закрыто: 0.3" in message
        assert "Остаток: 1" in message
        assert "Realized PnL: 0" in message
        with monitor.journal.path.open(
            encoding="utf-8",
            newline="",
        ) as stream:
            rows = list(csv.DictReader(stream))
        execution = next(
            row for row in rows
            if row["event_type"] == "TP2"
        )
        assert execution["quantity"] == "0.3"
        assert execution["take_profit"] == "45"
        assert execution["remaining_quantity"] == "1"
        assert execution["fee"] == "0"
        assert execution["funding"] == "0"

    asyncio.run(scenario())


def test_stop_loss_fill_reports_closed_position(tmp_path):
    async def scenario():
        notifications = []
        monitor = make_monitor(tmp_path, notifications)
        monitor.positions.get_open_positions = lambda *args: ()

        await monitor.handle_event({
            "ch": "tpsl",
            "ts": 300,
            "data": {
                "orderId": "sl-1",
                "positionId": "position-1",
                "symbol": "BTCUSDT",
                "side": "SELL",
                "status": "FILLED",
                "tpPrice": "",
                "slPrice": "55",
                "tpQty": "",
                "slQty": "1",
            },
        })

        message = notifications[0]
        assert "Исполнен SL" in message
        assert "Триггер: 55" in message
        assert "Закрыто: 1" in message
        assert "Остаток: позиция закрыта" in message
        with monitor.journal.path.open(
            encoding="utf-8",
            newline="",
        ) as stream:
            rows = list(csv.DictReader(stream))
        execution = next(
            row for row in rows
            if row["event_type"] == "SL"
        )
        assert execution["quantity"] == "1"
        assert execution["stop_loss"] == "55"

    asyncio.run(scenario())


def test_closed_position_writes_trade_summary(tmp_path):
    async def scenario():
        notifications = []
        monitor = make_monitor(tmp_path, notifications)

        await monitor.handle_event({
            "ch": "position",
            "ts": 400,
            "data": {
                "event": "CLOSE",
                "positionId": "position-1",
                "symbol": "BTCUSDT",
                "side": "SELL",
                "qty": "1",
                "avgOpenPrice": "50",
                "leverage": "10",
                "realizedPNL": "5",
                "fee": "0.2",
                "funding": "-0.1",
            },
        })

        with monitor.journal.path.open(
            encoding="utf-8",
            newline="",
        ) as stream:
            rows = list(csv.DictReader(stream))
        summary = next(
            row for row in rows
            if row["event_type"] == "TRADE_SUMMARY"
        )
        message = notifications[0]
        assert "Позиция закрыта: SHORT BTCUSDT" in message
        assert "Реализованный PnL: 5 USDT" in message
        assert "Комиссия: -0.2 USDT" in message
        assert "Funding: -0.1 USDT" in message
        assert "Чистый результат: 4.7 USDT" in message
        assert summary["status"] == "CLOSED"
        assert summary["side"] == "SHORT"
        assert summary["pnl"] == "5"
        assert summary["fee"] == "0.2"
        assert summary["funding"] == "-0.1"
        assert summary["net_pnl"] == "4.7"
        assert summary["remaining_quantity"] == "0"

    asyncio.run(scenario())


def test_closed_position_does_not_guess_missing_costs(tmp_path):
    async def scenario():
        notifications = []
        monitor = make_monitor(tmp_path, notifications)

        await monitor.handle_event({
            "ch": "position",
            "ts": 401,
            "data": {
                "event": "CLOSE",
                "positionId": "position-2",
                "symbol": "ETHUSDT",
                "side": "BUY",
                "realizedPNL": "2.5",
            },
        })

        message = notifications[0]
        assert "Позиция закрыта: LONG ETHUSDT" in message
        assert "Реализованный PnL: 2.5 USDT" in message
        assert "Комиссия: нет данных" in message
        assert "Funding: нет данных" in message
        assert "Чистый результат: нет данных" in message

    asyncio.run(scenario())


def test_reconciliation_warns_about_missing_protection(tmp_path):
    async def scenario():
        notifications = []
        monitor = make_monitor(tmp_path, notifications)

        await monitor.reconcile()

        assert "Отсутствует: SL" in notifications[0]

    asyncio.run(scenario())


def test_reconciliation_reports_active_limit_order_once(tmp_path):
    async def scenario():
        notifications = []
        monitor = make_monitor(tmp_path, notifications)
        monitor.register_plan("client-1", make_single_tp_plan())
        monitor.orders.get_order_detail = lambda client_id: {
            "status": "NEW_",
        }

        await monitor.reconcile()
        await monitor.reconcile()

        waiting_messages = [
            message
            for message in notifications
            if "Контроль лимитного входа активен" in message
        ]
        assert len(waiting_messages) == 1
        assert "TP и SL уже прикреплены на стороне Bitunix" in (
            waiting_messages[0]
        )

    asyncio.run(scenario())


def test_pending_order_wins_over_stale_filled_detail(tmp_path):
    async def scenario():
        notifications = []
        monitor = make_monitor(tmp_path, notifications)
        monitor.register_plan("client-1", make_single_tp_plan())
        monitor.orders.get_pending_orders = lambda: (
            SimpleNamespace(
                client_id="client-1",
                status="NEW_",
            ),
        )
        monitor.orders.get_order_detail = lambda client_id: {
            "status": "FILLED",
        }
        monitor.positions.get_open_positions = lambda *args: ()
        monitor.protections.unprotected_positions = (
            lambda positions, protections: ()
        )

        await monitor.reconcile()

        assert any(
            "Контроль лимитного входа активен" in message
            for message in notifications
        )
        assert not any(
            "позиция не появилась" in message
            for message in notifications
        )

    asyncio.run(scenario())


def test_reconciliation_does_not_claim_fill_without_position(tmp_path):
    async def scenario():
        notifications = []
        monitor = make_monitor(tmp_path, notifications)
        monitor.register_plan("client-1", make_single_tp_plan())
        monitor.orders.get_order_detail = lambda client_id: {
            "status": "FILLED",
        }
        monitor.positions.get_open_positions = lambda *args: ()
        monitor.protections.unprotected_positions = (
            lambda positions, protections: ()
        )

        await monitor.reconcile()

        assert not any(
            "позиция не появилась" in message
            for message in notifications
        )
        assert "client-1" in monitor._plans_by_client_id

    asyncio.run(scenario())


def test_filled_entry_only_verifies_attached_take_profits(tmp_path):
    async def scenario():
        notifications = []
        monitor = make_monitor(tmp_path, notifications)
        monitor.positions = FakePositions()
        monitor.positions.get_open_positions = (
            lambda *args: (
                replace(make_position(), quantity="2"),
            )
        )
        plan = TradePlan(
            symbol="BTCUSDT",
            side=OrderSide.LONG,
            entry_min=49,
            entry_max=51,
            current_price=50,
            in_range=True,
            total_quantity=2,
            stop_loss=45,
            take_profits=(
                PlannedTakeProfit(55, 1),
                PlannedTakeProfit(60, 0.6),
                PlannedTakeProfit(65, 0.4),
            ),
            leverage=10,
            risk_percent=1,
            risk_budget=10,
        )
        monitor.register_plan("client-1", plan)

        await monitor.handle_event({
            "ch": "order",
            "ts": 100,
            "data": {
                "orderId": "order-1",
                "clientId": "client-1",
                "symbol": "BTCUSDT",
                "orderStatus": "FILLED",
                "side": "BUY",
                "averagePrice": "50",
                "dealAmount": "2",
                "fee": "0.06",
            },
        })

        assert monitor.protections.placed == []
        assert any(
            "Прикреплённый TP2 не найден" in message
            for message in notifications
        )
        assert any(
            "Прикреплённый TP3 не найден" in message
            for message in notifications
        )
        assert "Защита BTCUSDT проверена: TP1 активен" in (
            notifications[-1]
        )
        assert monitor._tp1_order_positions == {
            "tp-existing": "position-1",
        }
        with monitor.journal.path.open(
            encoding="utf-8",
            newline="",
        ) as stream:
            rows = list(csv.DictReader(stream))
        entry = next(
            row for row in rows
            if row["event_type"] == "ENTRY"
        )
        assert entry["order_id"] == "order-1"

    asyncio.run(scenario())


def test_filled_entry_retries_until_position_is_visible(tmp_path):
    async def scenario():
        notifications = []
        monitor = make_monitor(tmp_path, notifications)
        monitor.positions = DelayedFakePositions(empty_responses=2)
        monitor.positions.position = replace(
            make_position(),
            quantity="2",
        )
        monitor.position_retry_delays = (0, 0)
        plan = TradePlan(
            symbol="BTCUSDT",
            side=OrderSide.LONG,
            entry_min=49,
            entry_max=51,
            current_price=50,
            in_range=True,
            total_quantity=2,
            stop_loss=45,
            take_profits=(
                PlannedTakeProfit(55, 1),
                PlannedTakeProfit(60, 0.6),
                PlannedTakeProfit(65, 0.4),
            ),
            leverage=10,
            risk_percent=1,
            risk_budget=10,
        )
        monitor.register_plan("client-1", plan)

        await monitor.handle_event({
            "ch": "order",
            "ts": 100,
            "data": {
                "orderId": "order-1",
                "clientId": "client-1",
                "symbol": "BTCUSDT",
                "orderStatus": "FILLED",
            },
        })

        assert monitor.positions.calls == 3
        assert monitor.protections.placed == []
        assert not any(
            "позиция не появилась" in message
            for message in notifications
        )

    asyncio.run(scenario())


def test_partial_entries_are_matched_to_their_own_positions(
    tmp_path,
    monkeypatch,
):
    async def run_synchronously(function, *args):
        return function(*args)

    monkeypatch.setattr(
        asyncio,
        "to_thread",
        run_synchronously,
    )

    async def scenario():
        notifications = []
        monitor = make_monitor(tmp_path, notifications)
        monitor.position_retry_delays = ()
        plan = TradePlan(
            symbol="BTCUSDT",
            side=OrderSide.LONG,
            entry_min=49,
            entry_max=51,
            current_price=50,
            in_range=True,
            total_quantity=3,
            stop_loss=45,
            take_profits=(
                PlannedTakeProfit(55, 1),
                PlannedTakeProfit(60, 1),
                PlannedTakeProfit(65, 1),
            ),
            leverage=10,
            risk_percent=1,
            risk_budget=10,
        )
        positions = (
            replace(
                make_position(),
                position_id="position-2",
                quantity="1",
            ),
            replace(
                make_position(),
                position_id="position-3",
                quantity="1",
            ),
        )
        monitor.positions.get_open_positions = lambda *args: positions
        monitor.protections.pending = [
            {
                "id": "tp-2",
                "positionId": "position-2",
                "tpPrice": "60",
                "tpQty": "1",
            },
            {
                "id": "tp-3",
                "positionId": "position-3",
                "tpPrice": "65",
                "tpQty": "1",
            },
        ]
        monitor.register_plan(
            "bot-execution-2",
            plan,
            tp_number=2,
        )
        monitor.register_plan(
            "bot-execution-3",
            plan,
            tp_number=3,
        )

        await monitor._verify_plan_protections(
            monitor._plans_by_client_id["bot-execution-3"],
            "bot-execution-3",
        )

        assert "bot-execution-2" in monitor._plans_by_client_id
        assert "bot-execution-3" not in monitor._plans_by_client_id
        assert monitor._tp_orders["tp-3"] == ("position-3", 3)
        assert "tp-3" not in monitor._tp1_order_positions
        assert notifications == []

    asyncio.run(scenario())


def test_filled_entry_warns_after_position_retries(tmp_path):
    async def scenario():
        notifications = []
        monitor = make_monitor(tmp_path, notifications)
        monitor.positions = DelayedFakePositions(empty_responses=10)
        monitor.position_retry_delays = (0, 0)
        plan = TradePlan(
            symbol="BTCUSDT",
            side=OrderSide.LONG,
            entry_min=49,
            entry_max=51,
            current_price=50,
            in_range=True,
            total_quantity=1,
            stop_loss=45,
            take_profits=(PlannedTakeProfit(55, 1),),
            leverage=10,
            risk_percent=1,
            risk_budget=10,
        )
        monitor.register_plan("client-1", plan)

        await monitor._verify_plan_protections(plan)

        assert monitor.positions.calls == 3
        assert monitor.protections.placed == []
        assert "повторных проверок" in notifications[-1]

    asyncio.run(scenario())


def test_existing_tp_must_match_position_and_quantity(tmp_path):
    async def scenario():
        notifications = []
        monitor = make_monitor(tmp_path, notifications)
        monitor.protections.pending = [{
            "id": "other-position-tp",
            "positionId": "position-2",
            "tpPrice": "55",
            "tpQty": "1",
        }]

        await monitor._verify_plan_protections(
            make_single_tp_plan()
        )

        assert monitor.protections.placed == []
        assert "Прикреплённый TP1 не найден" in notifications[-1]
        assert "other-position-tp" not in (
            monitor._tp1_order_positions
        )

        monitor.protections.placed.clear()
        monitor.protections.pending = [{
            "id": "wrong-quantity-tp",
            "positionId": "position-1",
            "tpPrice": "55",
            "tpQty": "0.5",
        }]

        await monitor._verify_plan_protections(
            make_single_tp_plan()
        )

        assert monitor.protections.placed == []
        assert "объём не совпадает" in notifications[-1]

    asyncio.run(scenario())


def test_new_tp_cannot_exceed_remaining_position(tmp_path):
    async def scenario():
        notifications = []
        monitor = make_monitor(tmp_path, notifications)
        monitor.protections.pending = [{
            "id": "existing-tp",
            "positionId": "position-1",
            "tpPrice": "60",
            "tpQty": "0.8",
        }]

        plan = replace(
            make_single_tp_plan(quantity=0.3),
            total_quantity=1,
        )
        await monitor._verify_plan_protections(plan)

        assert monitor.protections.placed == []
        assert "превышает остаток позиции" in notifications[-1]

    asyncio.run(scenario())


def test_tp_quantity_comparison_allows_decimal_noise(tmp_path):
    async def scenario():
        notifications = []
        monitor = make_monitor(tmp_path, notifications)
        monitor.protections.pending = [{
            "id": "existing-tp",
            "positionId": "position-1",
            "tpPrice": "55",
            "tpQty": "1.0000000001",
        }]

        await monitor._verify_plan_protections(
            make_single_tp_plan()
        )

        assert monitor.protections.placed == []
        assert monitor._tp1_order_positions == {
            "existing-tp": "position-1",
        }
        assert "Проверены прикреплённые TP: 1" in (
            notifications[-1]
        )

    asyncio.run(scenario())


def test_total_tp_quantity_allows_only_tiny_decimal_noise(tmp_path):
    async def scenario():
        notifications = []
        monitor = make_monitor(tmp_path, notifications)
        monitor.protections.pending = [{
            "id": "existing-tp",
            "positionId": "position-1",
            "tpPrice": "60",
            "tpQty": "0.7000000001",
        }]

        plan = replace(
            make_single_tp_plan(quantity=0.3),
            total_quantity=1,
        )
        await monitor._verify_plan_protections(plan)

        assert monitor.protections.placed == []
        assert any(
            "Прикреплённый TP1 не найден" in message
            for message in notifications
        )
        assert not any(
            "превышает остаток позиции" in message
            for message in notifications
        )

    asyncio.run(scenario())


def test_tp1_fill_requests_confirmation_before_moving_stop(
    tmp_path,
    run_blocking_calls_inline,
):
    async def scenario():
        notifications = []
        monitor = make_monitor(tmp_path, notifications)
        monitor._tp1_order_positions["tp-1"] = "position-1"
        monitor.protections.pending = [{
            "id": "sl-1",
            "positionId": "position-1",
            "tpPrice": "",
            "slPrice": "45",
            "slQty": "1",
        }]

        await monitor.handle_event({
            "ch": "tpsl",
            "ts": 200,
            "data": {
                "orderId": "tp-1",
                "positionId": "position-1",
                "symbol": "BTCUSDT",
                "status": "FILLED",
                "tpPrice": "55",
            },
        })

        assert monitor.protections.modified == []
        assert "🎯 TP1 исполнен" in notifications[-1]
        assert "Оставшихся позиций: 1" in notifications[-1]
        assert "position-1: объём 1" in notifications[-1]
        assert "SL 45 → 50.03001803" in notifications[-1]
        proposal_id = next(iter(monitor._break_even_proposals))

        result = await monitor.confirm_break_even(
            proposal_id,
            True,
        )

        assert monitor.protections.modified == [
            ("sl-1", "50.03001803", "1")
        ]
        assert "50.03001803" in result

    asyncio.run(scenario())


def test_tp1_can_move_stop_to_break_even_automatically(
    tmp_path,
    run_blocking_calls_inline,
):
    async def scenario():
        notifications = []
        monitor = make_monitor(
            tmp_path,
            notifications,
            auto_move_stop_loss_on_tp1=True,
        )
        monitor._tp1_order_positions["tp-1"] = "position-1"
        monitor.protections.pending = [{
            "id": "sl-1",
            "positionId": "position-1",
            "slPrice": "45",
            "slQty": "1",
        }]

        await monitor._request_break_even({
            "orderId": "tp-1",
            "symbol": "BTCUSDT",
        })

        assert monitor.protections.modified == [
            ("sl-1", "50.03001803", "1")
        ]
        assert monitor._break_even_proposals == {}
        assert "Автоматический перенос SL выполнен" in (
            notifications[-1]
        )
        assert "50.03001803" in notifications[-1]

    asyncio.run(scenario())


def test_break_even_preserves_each_partial_stop_quantity(
    tmp_path,
    run_blocking_calls_inline,
):
    async def scenario():
        notifications = []
        monitor = make_monitor(tmp_path, notifications)
        monitor._tp1_order_positions["tp-1"] = "position-1"
        monitor.protections.pending = [
            {
                "id": "sl-2",
                "positionId": "position-1",
                "slPrice": "45",
                "slQty": "0.6",
            },
            {
                "id": "sl-3",
                "positionId": "position-1",
                "slPrice": "45",
                "slQty": "0.4",
            },
        ]

        await monitor._request_break_even({"orderId": "tp-1"})
        proposal_id = next(iter(monitor._break_even_proposals))

        await monitor.confirm_break_even(proposal_id, True)

        assert monitor.protections.modified == [
            ("sl-2", "50.03001803", "0.6"),
            ("sl-3", "50.03001803", "0.4"),
        ]

    asyncio.run(scenario())


def test_break_even_confirmation_can_be_cancelled(
    tmp_path,
    run_blocking_calls_inline,
):
    async def scenario():
        notifications = []
        monitor = make_monitor(tmp_path, notifications)
        monitor._tp1_order_positions["tp-1"] = "position-1"
        monitor.protections.pending = [{
            "id": "sl-1",
            "positionId": "position-1",
            "slPrice": "45",
        }]

        await monitor._request_break_even({"orderId": "tp-1"})
        proposal_id = next(iter(monitor._break_even_proposals))
        result = await monitor.confirm_break_even(
            proposal_id,
            False,
        )

        assert result == "SL оставлен без изменений"
        assert monitor.protections.modified == []

    asyncio.run(scenario())


def test_break_even_confirmation_expires_and_is_one_time(
    tmp_path,
    run_blocking_calls_inline,
):
    async def scenario():
        notifications = []
        monitor = make_monitor(tmp_path, notifications)
        monitor._tp1_order_positions["tp-1"] = "position-1"
        monitor.protections.pending = [{
            "id": "sl-1",
            "positionId": "position-1",
            "slPrice": "45",
        }]

        await monitor._request_break_even({"orderId": "tp-1"})
        proposal_id = next(iter(monitor._break_even_proposals))
        expires_at = monitor._break_even_proposals[
            proposal_id
        ].expires_at

        try:
            await monitor.confirm_break_even(
                proposal_id,
                True,
                now=expires_at,
            )
        except ValueError as error:
            assert str(error) == "Время подтверждения истекло"
        else:
            raise AssertionError("Expired confirmation was accepted")

        try:
            await monitor.confirm_break_even(proposal_id, True)
        except ValueError as error:
            assert "уже использовано" in str(error)
        else:
            raise AssertionError("Confirmation was reused")

        assert monitor.protections.modified == []

    asyncio.run(scenario())


def test_break_even_groups_only_remaining_positions_of_same_signal(
    tmp_path,
    run_blocking_calls_inline,
):
    async def scenario():
        notifications = []
        monitor = make_monitor(tmp_path, notifications)
        first = make_position()
        second = replace(
            first,
            position_id="position-2",
            quantity="0.3",
            fee="0.01",
        )
        third = replace(
            first,
            position_id="position-3",
            quantity="0.2",
            fee="0.02",
        )
        unrelated = replace(
            first,
            position_id="position-other",
            quantity="0.4",
        )
        monitor.positions.get_open_positions = lambda *args: (
            second,
            third,
            unrelated,
        )
        monitor._tp_orders = {
            "tp-1": ("position-1", 1),
            "tp-2": ("position-2", 2),
            "tp-3": ("position-3", 3),
            "other-tp": ("position-other", 2),
        }
        monitor._tp1_order_positions = {
            "tp-1": "position-1",
        }
        monitor._tp_order_client_ids = {
            "tp-1": "bot-signal-a-1",
            "tp-2": "bot-signal-a-2",
            "tp-3": "bot-signal-a-3",
            "other-tp": "bot-signal-b-2",
        }
        monitor.protections.pending = [
            {
                "id": "sl-2",
                "positionId": "position-2",
                "slPrice": "45",
                "slQty": "0.3",
            },
            {
                "id": "sl-3",
                "positionId": "position-3",
                "slPrice": "45",
                "slQty": "0.2",
            },
            {
                "id": "sl-other",
                "positionId": "position-other",
                "slPrice": "45",
                "slQty": "0.4",
            },
        ]

        await monitor._request_break_even({
            "orderId": "tp-1",
            "symbol": "BTCUSDT",
        })
        proposal_id = next(iter(monitor._break_even_proposals))
        proposal = monitor._break_even_proposals[proposal_id]

        assert proposal.position_ids == (
            "position-2",
            "position-3",
        )
        assert "position-other" not in notifications[-1]

        result = await monitor.confirm_break_even(proposal_id, True)

        assert [item[0] for item in monitor.protections.modified] == [
            "sl-2",
            "sl-3",
        ]
        assert "position-2" in result
        assert "position-3" in result
        assert "position-other" not in result

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("filled_order", "remaining_position", "target", "label"),
    [
        ("tp-2", "position-3", "55", "TP1"),
        ("tp-3", "position-4", "60", "TP2"),
    ],
)
def test_later_tp_moves_remaining_stops_to_previous_tp(
    tmp_path,
    run_blocking_calls_inline,
    filled_order,
    remaining_position,
    target,
    label,
):
    async def scenario():
        notifications = []
        monitor = make_monitor(
            tmp_path,
            notifications,
            auto_move_stop_loss_on_tp1=True,
        )
        position = replace(
            make_position(),
            position_id=remaining_position,
            quantity="0.2",
        )
        monitor.positions.get_open_positions = lambda *args: (position,)
        monitor._tp_orders = {
            "tp-1": ("position-1", 1),
            "tp-2": ("position-2", 2),
            "tp-3": ("position-3", 3),
            "tp-4": ("position-4", 4),
        }
        monitor._tp_order_client_ids = {
            "tp-1": "bot-signal-a-1",
            "tp-2": "bot-signal-a-2",
            "tp-3": "bot-signal-a-3",
            "tp-4": "bot-signal-a-4",
        }
        monitor._tp_order_prices = {
            "tp-1": Decimal("55"),
            "tp-2": Decimal("60"),
            "tp-3": Decimal("65"),
            "tp-4": Decimal("70"),
        }
        monitor.protections.pending = [{
            "id": "sl-3",
            "positionId": remaining_position,
            "slPrice": "50.03",
            "slQty": "0.2",
        }]

        await monitor._handle_take_profit_stop_move({
            "orderId": filled_order,
            "symbol": "BTCUSDT",
        })

        assert monitor.protections.modified == [
            ("sl-3", target, "0.2")
        ]
        assert f"SL оставшихся частей перенесён на {label}: {target}" in (
            notifications[-1]
        )

    asyncio.run(scenario())


def test_later_take_profit_never_moves_stop_backwards(
    tmp_path,
    run_blocking_calls_inline,
):
    async def scenario():
        notifications = []
        monitor = make_monitor(
            tmp_path,
            notifications,
            auto_move_stop_loss_on_tp1=True,
        )
        position = replace(
            make_position(),
            position_id="position-3",
        )
        monitor.positions.get_open_positions = lambda *args: (position,)
        monitor._tp_orders = {
            "tp-1": ("position-1", 1),
            "tp-2": ("position-2", 2),
            "tp-3": ("position-3", 3),
        }
        monitor._tp_order_client_ids = {
            "tp-1": "bot-signal-a-1",
            "tp-2": "bot-signal-a-2",
            "tp-3": "bot-signal-a-3",
        }
        monitor._tp_order_prices = {"tp-1": Decimal("55")}
        monitor.protections.pending = [{
            "id": "sl-3",
            "positionId": "position-3",
            "slPrice": "56",
            "slQty": "1",
        }]

        await monitor._handle_take_profit_stop_move({
            "orderId": "tp-2",
            "symbol": "BTCUSDT",
        })

        assert monitor.protections.modified == []
        assert "Позиций защищено: 1" in notifications[-1]

    asyncio.run(scenario())
