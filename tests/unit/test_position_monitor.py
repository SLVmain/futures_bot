import asyncio
import csv
from dataclasses import replace

from models.signal import OrderSide
from models.trade import PlannedTakeProfit, TradePlan
from services.position_monitor import PositionMonitor
from services.trade_journal import CsvTradeJournal
from tests.unit.test_protection_service import make_position


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


def make_monitor(tmp_path, notifications):
    async def notify(message, action_id=None):
        notifications.append(message)

    return PositionMonitor(
        FakeOrders(),
        FakePositions(),
        FakeProtections(),
        CsvTradeJournal(tmp_path / "journal.csv"),
        notify,
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
        assert "Исполнен входной ордер" in notifications[0]
        assert "Позиция: LONG BTCUSDT" in notifications[0]
        assert "Средняя цена: 50.1" in notifications[0]
        assert "Исполнено: 1" in notifications[0]
        assert "Комиссия: 0.03" in notifications[0]

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
        assert summary["status"] == "CLOSED"
        assert summary["side"] == "SHORT"
        assert summary["pnl"] == "5"
        assert summary["fee"] == "0.2"
        assert summary["funding"] == "-0.1"
        assert summary["net_pnl"] == "4.7"
        assert summary["remaining_quantity"] == "0"

    asyncio.run(scenario())


def test_reconciliation_warns_about_missing_protection(tmp_path):
    async def scenario():
        notifications = []
        monitor = make_monitor(tmp_path, notifications)

        await monitor.reconcile()

        assert "Отсутствует: SL" in notifications[0]

    asyncio.run(scenario())


def test_filled_entry_adds_all_partial_take_profits(tmp_path):
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
            },
        })

        assert monitor.protections.placed == [
            ("BTCUSDT", "position-1", 60, 0.6),
            ("BTCUSDT", "position-1", 65, 0.4),
        ]
        assert "Добавлено частичных TP: 2" in notifications[-2]
        assert "Восстановлены ID существующих TP: 1" in notifications[-1]
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
        assert len(monitor.protections.placed) == 2
        assert not any(
            "позиция не появилась" in message
            for message in notifications
        )

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

        await monitor._install_plan(plan)

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

        await monitor._install_plan(make_single_tp_plan())

        assert monitor.protections.placed == [
            ("BTCUSDT", "position-1", 55, 1),
        ]
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

        await monitor._install_plan(make_single_tp_plan())

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

        await monitor._install_plan(
            make_single_tp_plan(quantity=0.3)
        )

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

        await monitor._install_plan(make_single_tp_plan())

        assert monitor.protections.placed == []
        assert monitor._tp1_order_positions == {
            "existing-tp": "position-1",
        }
        assert "Восстановлены ID существующих TP: 1" in (
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

        await monitor._install_plan(
            make_single_tp_plan(quantity=0.3)
        )

        assert monitor.protections.placed == [
            ("BTCUSDT", "position-1", 55, 0.3),
        ]
        assert not any(
            "превышает остаток позиции" in message
            for message in notifications
        )

    asyncio.run(scenario())


def test_tp1_fill_requests_confirmation_before_moving_stop(tmp_path):
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
        assert "Остаток: 1" in notifications[-1]
        assert "Средняя цена: 50" in notifications[-1]
        assert "Текущий SL: 45" in notifications[-1]
        assert "Предлагаемый SL: 50.03001803" in notifications[-1]
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


def test_break_even_confirmation_can_be_cancelled(tmp_path):
    async def scenario():
        notifications = []
        monitor = make_monitor(tmp_path, notifications)
        monitor._tp1_order_positions["tp-1"] = "position-1"
        monitor.protections.pending = [{
            "id": "sl-1",
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


def test_break_even_confirmation_expires_and_is_one_time(tmp_path):
    async def scenario():
        notifications = []
        monitor = make_monitor(tmp_path, notifications)
        monitor._tp1_order_positions["tp-1"] = "position-1"
        monitor.protections.pending = [{
            "id": "sl-1",
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
