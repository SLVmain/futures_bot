import asyncio
from types import SimpleNamespace

from models.management import ManagementAction, ManagementProposal
from services.management_proposal_service import ManagementProposalService
from telegram_bot import FuturesBot


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text=None, **kwargs):
        self.replies.append((text, kwargs))


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.edits = []
        self.answered = False

    async def answer(self):
        self.answered = True

    async def edit_message_text(self, text=None, **kwargs):
        self.edits.append((text, kwargs))


class FakeUpdate:
    def __init__(self, callback_data=None):
        self.message = FakeMessage()
        self.callback_query = (
            FakeQuery(callback_data)
            if callback_data is not None
            else None
        )


class FakePositionService:
    def __init__(self, positions):
        self.positions = positions
        self.close_calls = []

    def get_open_positions(self, symbol=None, position_id=None):
        return tuple(
            position
            for position in self.positions
            if (symbol is None or position.symbol == symbol)
            and (
                position_id is None
                or position.position_id == position_id
            )
        )

    def close_position(self, position_id):
        self.close_calls.append(position_id)
        return SimpleNamespace(simulated=True)


class FakeOrderService:
    def __init__(self, orders):
        self.orders = orders
        self.cancel_calls = []

    def get_pending_orders(
        self,
        symbol=None,
        order_id=None,
        client_id=None,
        status=None,
        skip=0,
        limit=100,
    ):
        return tuple(
            order
            for order in self.orders
            if (symbol is None or order.symbol == symbol)
            and (order_id is None or order.order_id == order_id)
        )

    def cancel_orders(self, symbol, order_ids):
        self.cancel_calls.append((symbol, order_ids))
        return SimpleNamespace(simulated=True)


def make_bot(*, positions=(), orders=()):
    bot = FuturesBot.__new__(FuturesBot)
    bot.position_service = FakePositionService(positions)
    bot.order_service = FakeOrderService(orders)

    async def authorize(update):
        return True

    bot._authorize = authorize
    return bot


def make_position():
    return SimpleNamespace(
        position_id="position-1",
        symbol="TAOUSDT",
        side="SHORT",
        quantity="0.654",
        average_open_price="193.53",
        unrealized_pnl="4.25",
    )


def make_order():
    return SimpleNamespace(
        order_id="order-1",
        symbol="TAOUSDT",
        side="BUY",
        quantity="0.261",
        price="189.33",
        order_type="LIMIT",
        status="NEW",
    )


def test_close_position_without_id_shows_position_buttons():
    async def scenario():
        bot = make_bot(positions=(make_position(),))
        update = FakeUpdate()
        context = SimpleNamespace(args=[], user_data={})

        await bot.close_position(update, context)

        text, kwargs = update.message.replies[0]
        assert text == "Какую позицию закрыть полностью?"
        button = kwargs["reply_markup"].inline_keyboard[0][0]
        assert "SHORT TAOUSDT" in button.text
        assert "PnL 4.25" in button.text
        assert (
            button.callback_data
            == "manage:select_position:position-1"
        )

    asyncio.run(scenario())


def test_cancel_order_without_id_shows_order_buttons():
    async def scenario():
        bot = make_bot(orders=(make_order(),))
        update = FakeUpdate()
        context = SimpleNamespace(args=[], user_data={})

        await bot.cancel_order(update, context)

        text, kwargs = update.message.replies[0]
        assert text == "Какой ордер отменить?"
        button = kwargs["reply_markup"].inline_keyboard[0][0]
        assert "BUY TAOUSDT" in button.text
        assert "0.261 @ 189.33" in button.text
        assert button.callback_data == "manage:select_order:order-1"

    asyncio.run(scenario())


def test_position_selection_shows_details_and_confirmation():
    async def scenario():
        bot = make_bot(positions=(make_position(),))
        update = FakeUpdate("manage:select_position:position-1")
        context = SimpleNamespace(args=[], user_data={})

        await bot.management_button_handler(update, context)

        text, kwargs = update.callback_query.edits[0]
        assert "Полностью закрыть позицию?" in text
        assert "SHORT" in text
        assert "Нереализованный PnL: 4.25" in text
        proposal = context.user_data["management_proposal"]
        assert proposal.action is ManagementAction.CLOSE_POSITION
        assert proposal.target_id == "position-1"
        assert proposal.symbol == "TAOUSDT"
        confirm = kwargs["reply_markup"].inline_keyboard[0][0]
        assert confirm.callback_data.endswith(proposal.proposal_id)

    asyncio.run(scenario())


def test_disappeared_position_is_not_closed_after_confirmation():
    async def scenario():
        bot = make_bot(positions=())
        proposal = ManagementProposal.create(
            ManagementAction.CLOSE_POSITION,
            "position-1",
            symbol="TAOUSDT",
        )
        user_data = {}
        ManagementProposalService.store(user_data, proposal)
        update = FakeUpdate(
            f"manage:confirm:{proposal.proposal_id}"
        )
        context = SimpleNamespace(args=[], user_data=user_data)

        await bot.management_button_handler(update, context)

        assert bot.position_service.close_calls == []
        assert "Закрытие не отправлено" in (
            update.callback_query.edits[-1][0]
        )

    asyncio.run(scenario())


def test_existing_order_is_rechecked_and_cancelled():
    async def scenario():
        bot = make_bot(orders=(make_order(),))
        proposal = ManagementProposal.create(
            ManagementAction.CANCEL_ORDER,
            "order-1",
            symbol="TAOUSDT",
        )
        user_data = {}
        ManagementProposalService.store(user_data, proposal)
        update = FakeUpdate(
            f"manage:confirm:{proposal.proposal_id}"
        )
        context = SimpleNamespace(args=[], user_data=user_data)

        await bot.management_button_handler(update, context)

        assert bot.order_service.cancel_calls == [
            ("TAOUSDT", ("order-1",))
        ]
        assert "симулирована" in update.callback_query.edits[-1][0]

    asyncio.run(scenario())
