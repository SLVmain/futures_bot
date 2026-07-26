import asyncio

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


class FakeProtections:
    def __init__(self):
        self.placed = []
        self.modified = []
        self.pending = [{
            "positionId": "position-1",
            "tpPrice": "55",
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
    async def notify(message):
        notifications.append(message)

    return PositionMonitor(
        FakeOrders(),
        FakePositions(),
        FakeProtections(),
        CsvTradeJournal(tmp_path / "journal.csv"),
        notify,
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
            },
        }

        await monitor.handle_event(event)
        await monitor.handle_event(event)

        assert notifications == [
            "ℹ️ Ордер исполнен: BTCUSDT, ID order-1"
        ]

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
        assert "Добавлено частичных TP: 2" in notifications[-1]

    asyncio.run(scenario())


def test_tp1_fill_moves_stop_to_average_entry(tmp_path):
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

        assert monitor.protections.modified == [
            ("sl-1", "50.03001803", "1")
        ]
        assert (
            "SL перенесён в безубыток: 50.03001803"
            in notifications[-1]
        )

    asyncio.run(scenario())
