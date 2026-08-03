import asyncio
import csv
from pathlib import Path
from types import SimpleNamespace

from models.signal_update import SignalUpdateType
from services.signal_update_parser import SignalUpdateParser
from services.trade_journal import CsvTradeJournal
from telegram.ext import ConversationHandler
from telegram_bot import FuturesBot


class FakeMessage:
    def __init__(self, text, message_id=10, photo=()):
        self.text = text
        self.caption = None
        self.message_id = message_id
        self.photo = photo
        self.replies = []

    async def reply_text(self, text=None, **kwargs):
        self.replies.append((text, kwargs))


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.edits = []

    async def answer(self):
        return None

    async def edit_message_text(self, text=None, **kwargs):
        self.edits.append((text, kwargs))


class FakeUpdate:
    def __init__(self, text="", callback_data=None, message_id=10):
        self.message = FakeMessage(text, message_id)
        self.effective_message = self.message
        self.effective_chat = SimpleNamespace(id=123)
        self.callback_query = (
            FakeQuery(callback_data) if callback_data else None
        )


class FakeOrders:
    def __init__(self, orders=(), error=None, cancel_failures=()):
        self.orders = tuple(orders)
        self.error = error
        self.cancel_failures = tuple(cancel_failures)
        self.cancel_calls = []

    def get_pending_orders(self, symbol=None, *args, **kwargs):
        if self.error:
            raise self.error
        return tuple(
            item
            for item in self.orders
            if symbol is None or item.symbol == symbol
        )

    def cancel_orders(self, symbol, order_ids):
        self.cancel_calls.append((symbol, order_ids))
        return SimpleNamespace(
            simulated=True,
            failed=self.cancel_failures,
        )


class FakePositions:
    def __init__(self, positions=()):
        self.positions = tuple(positions)
        self.close_calls = []

    def get_open_positions(self, symbol=None, *args):
        return tuple(
            item
            for item in self.positions
            if symbol is None or item.symbol == symbol
        )

    def close_position(self, position_id):
        self.close_calls.append(position_id)
        return SimpleNamespace(simulated=True)


def make_order(*, client_id="bot-execution-1"):
    return SimpleNamespace(
        order_id="order-1",
        client_id=client_id,
        symbol="PUMPUSDT",
        reduce_only=False,
        side="SELL",
        quantity="10",
        price="0.0022",
        order_type="LIMIT",
    )


def make_position():
    return SimpleNamespace(
        position_id="position-1",
        symbol="PUMPUSDT",
        side="LONG",
        quantity="10",
        average_open_price="0.0023",
        unrealized_pnl="-0.5",
    )


def make_bot(
    tmp_path: Path,
    *,
    orders=(),
    positions=(),
    error=None,
    cancel_failures=(),
):
    bot = FuturesBot.__new__(FuturesBot)
    bot.order_service = FakeOrders(orders, error, cancel_failures)
    bot.position_service = FakePositions(positions)
    bot.monitor = None
    bot.monitoring = SimpleNamespace(auto_break_even_on_tp1=True)
    bot.journal = CsvTradeJournal(tmp_path / "journal.csv")
    bot._recent_signal_updates = {}

    async def authorize(update):
        return True

    bot._authorize = authorize
    return bot


def parse(text):
    result = SignalUpdateParser.parse(text)
    assert result is not None
    return result


def journal_statuses(path):
    with path.open(encoding="utf-8", newline="") as stream:
        return [row["status"] for row in csv.DictReader(stream)]


def test_missing_symbol_exposure_is_informational(tmp_path):
    async def scenario():
        bot = make_bot(tmp_path)
        update = FakeUpdate("#XRP отмена, цена ушла")
        context = SimpleNamespace(user_data={})

        await bot._process_signal_update(
            update,
            context,
            parse(update.message.text),
        )

        assert "Открытых позиций и активных ордеров не найдено" in (
            update.message.replies[-1][0]
        )
        assert bot.order_service.cancel_calls == []
        assert bot.position_service.close_calls == []
        assert journal_statuses(bot.journal.path) == [
            "RECEIVED",
            "NO_ACTIVE_TRADE",
        ]

    asyncio.run(scenario())


def test_manual_exposure_is_proposed_for_management(tmp_path):
    async def scenario():
        bot = make_bot(
            tmp_path,
            orders=(make_order(client_id=None),),
            positions=(make_position(),),
        )
        update = FakeUpdate("#PUMP закрываю идею по текущим ценам")
        context = SimpleNamespace(user_data={})

        await bot._process_signal_update(
            update,
            context,
            parse(update.message.text),
        )

        proposal = context.user_data["management_proposal"]
        assert proposal.order_ids == ("order-1",)
        assert proposal.position_ids == ("position-1",)
        assert bot.order_service.cancel_calls == []
        assert bot.position_service.close_calls == []

        confirmation = FakeUpdate(
            callback_data=f"manage:confirm:{proposal.proposal_id}"
        )
        await bot.management_button_handler(confirmation, context)

        assert bot.order_service.cancel_calls == [
            ("PUMPUSDT", ("order-1",))
        ]
        assert bot.position_service.close_calls == ["position-1"]

    asyncio.run(scenario())


def test_opposite_signal_proposes_and_executes_combined_action(tmp_path):
    async def scenario():
        bot = make_bot(
            tmp_path,
            orders=(make_order(),),
            positions=(make_position(),),
        )
        text = (
            "❗️#PUMPUSDT.P Закрыто из-за противоположной сделки!\n"
            "Цена закрытия: 0.002217\nПрибыль: -7.15%"
        )
        update = FakeUpdate(text)
        context = SimpleNamespace(user_data={})

        await bot._process_signal_update(
            update,
            context,
            parse(text),
        )

        proposal = context.user_data["management_proposal"]
        assert proposal.signal_event_type == (
            SignalUpdateType.CLOSE_OPPOSITE.value
        )
        assert proposal.order_ids == ("order-1",)
        assert proposal.position_ids == ("position-1",)
        assert "Цена автора: 0.002217" in update.message.replies[-1][0]
        assert "Результат автора: -7.15%" in update.message.replies[-1][0]

        confirmation = FakeUpdate(
            callback_data=f"manage:confirm:{proposal.proposal_id}"
        )
        await bot.management_button_handler(confirmation, context)

        assert bot.order_service.cancel_calls == [
            ("PUMPUSDT", ("order-1",))
        ]
        assert bot.position_service.close_calls == ["position-1"]
        assert "Сопровождение выполнено" in (
            confirmation.callback_query.edits[-1][0]
        )

    asyncio.run(scenario())


def test_cancel_signal_proposes_all_positions_and_entries(tmp_path):
    async def scenario():
        bot = make_bot(
            tmp_path,
            orders=(make_order(),),
            positions=(make_position(),),
        )
        update = FakeUpdate("#PUMP отмена, цена ушла")
        context = SimpleNamespace(user_data={})

        await bot._process_signal_update(
            update,
            context,
            parse(update.message.text),
        )

        proposal = context.user_data["management_proposal"]
        assert proposal.order_ids == ("order-1",)
        assert proposal.position_ids == ("position-1",)
        assert bot.order_service.cancel_calls == []
        assert bot.position_service.close_calls == []

        confirmation = FakeUpdate(
            callback_data=f"manage:confirm:{proposal.proposal_id}"
        )
        await bot.management_button_handler(confirmation, context)

        assert bot.order_service.cancel_calls == [
            ("PUMPUSDT", ("order-1",))
        ]
        assert bot.position_service.close_calls == ["position-1"]

    asyncio.run(scenario())


def test_failed_entry_cancellation_stops_combined_close(tmp_path):
    async def scenario():
        bot = make_bot(
            tmp_path,
            orders=(make_order(),),
            positions=(make_position(),),
            cancel_failures=(SimpleNamespace(),),
        )
        text = "#PUMP закрыто из-за противоположной сделки"
        update = FakeUpdate(text)
        context = SimpleNamespace(user_data={})
        await bot._process_signal_update(
            update,
            context,
            parse(text),
        )
        proposal = context.user_data["management_proposal"]
        confirmation = FakeUpdate(
            callback_data=f"manage:confirm:{proposal.proposal_id}"
        )

        await bot.management_button_handler(confirmation, context)

        assert bot.position_service.close_calls == []
        assert "Закрытие позиции не отправлялось" in (
            confirmation.callback_query.edits[-1][0]
        )
        assert "ACTION_PARTIAL" in journal_statuses(bot.journal.path)

    asyncio.run(scenario())


def test_stop_report_proposes_close_but_does_not_execute_it(tmp_path):
    async def scenario():
        bot = make_bot(
            tmp_path,
            positions=(make_position(),),
        )
        update = FakeUpdate(
            "#PUMP идея закрывается по стопу (-1%)"
        )
        context = SimpleNamespace(user_data={})

        await bot._process_signal_update(
            update,
            context,
            parse(update.message.text),
        )

        proposal = context.user_data["management_proposal"]
        assert proposal.position_ids == ("position-1",)
        assert bot.position_service.close_calls == []
        assert "Подтвердите в течение 5 минут" in (
            update.message.replies[-1][0]
        )

    asyncio.run(scenario())


def test_reported_tp1_does_not_move_stop_or_create_proposal(tmp_path):
    async def scenario():
        bot = make_bot(
            tmp_path,
            positions=(make_position(),),
        )
        update = FakeUpdate(
            "#PUMP была достигнута первая цель (+1,13%)"
        )
        context = SimpleNamespace(user_data={})

        await bot._process_signal_update(
            update,
            context,
            parse(update.message.text),
        )

        assert "WebSocket-событию Bitunix" in update.message.replies[-1][0]
        assert "management_proposal" not in context.user_data
        assert bot.position_service.close_calls == []

    asyncio.run(scenario())


def test_duplicate_update_is_not_processed_twice(tmp_path):
    async def scenario():
        bot = make_bot(tmp_path)
        context = SimpleNamespace(user_data={})
        signal = parse("#BNB отмена")
        first = FakeUpdate("#BNB отмена", message_id=1)
        second = FakeUpdate("#BNB отмена", message_id=2)

        await bot._process_signal_update(first, context, signal)
        await bot._process_signal_update(second, context, signal)

        assert "уже было обработано" in second.message.replies[-1][0]
        assert journal_statuses(bot.journal.path) == [
            "RECEIVED",
            "NO_ACTIVE_TRADE",
        ]

    asyncio.run(scenario())


def test_api_error_is_not_reported_as_missing_trade(tmp_path):
    async def scenario():
        bot = make_bot(tmp_path, error=ConnectionError())
        update = FakeUpdate("#XRP отмена")

        await bot._process_signal_update(
            update,
            SimpleNamespace(user_data={}),
            parse(update.message.text),
        )

        assert "Не удалось проверить состояние Bitunix" in (
            update.message.replies[-1][0]
        )
        assert "не привёл к отмене или закрытию" in (
            update.message.replies[-1][0]
        )

    asyncio.run(scenario())


def test_photo_without_caption_is_ignored_without_reset(tmp_path):
    async def scenario():
        bot = make_bot(tmp_path)
        update = FakeUpdate("")
        update.message.photo = (SimpleNamespace(),)
        context = SimpleNamespace(user_data={"existing": "state"})

        result = await bot.handle_signal(update, context)

        assert result == ConversationHandler.END
        assert update.message.replies == []
        assert context.user_data == {"existing": "state"}

    asyncio.run(scenario())
