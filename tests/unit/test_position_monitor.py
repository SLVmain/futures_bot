import asyncio

from services.position_monitor import PositionMonitor
from services.trade_journal import CsvTradeJournal
from tests.unit.test_protection_service import make_position


class FakeOrders:
    def get_pending_orders(self):
        return ()


class FakePositions:
    def get_open_positions(self):
        return (make_position(),)


class FakeProtections:
    def get_pending_tp_sl(self):
        return ({
            "positionId": "position-1",
            "tpPrice": "55",
            "slPrice": "",
        },)

    def unprotected_positions(self, positions, protections):
        return ((positions[0], ("SL",)),)


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
