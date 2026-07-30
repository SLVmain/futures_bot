import asyncio
from types import SimpleNamespace

from telegram.ext import ConversationHandler

from telegram_bot import (
    LEVERAGE_KEY,
    WAITING_LEVERAGE,
    WAITING_RISK,
    FuturesBot,
)


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        reply = FakeProgressMessage(text)
        self.replies.append((text, kwargs, reply))
        return reply


class FakeProgressMessage:
    def __init__(self, text):
        self.text = text
        self.edits = []

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.message = FakeMessage()
        self.answered = False
        self.reply_markup_edits = []

    async def answer(self):
        self.answered = True

    async def edit_message_reply_markup(self, **kwargs):
        self.reply_markup_edits.append(kwargs)


def make_bot():
    bot = FuturesBot.__new__(FuturesBot)

    async def authorize(update):
        return True

    bot._authorize = authorize
    return bot


def test_leverage_and_risk_keyboards_have_only_requested_options():
    leverage_buttons = FuturesBot._leverage_keyboard().inline_keyboard
    risk_buttons = FuturesBot._risk_keyboard().inline_keyboard

    assert [
        button.callback_data
        for button in leverage_buttons[0]
    ] == ["leverage:10", "leverage:15", "leverage:20"]
    assert leverage_buttons[1][0].callback_data == "leverage:manual"
    assert [
        button.callback_data
        for button in risk_buttons[0]
    ] == ["risk:1", "risk:2", "risk:3"]


def test_leverage_button_stores_value_and_shows_risk_buttons():
    async def scenario():
        bot = make_bot()
        query = FakeQuery("leverage:15")
        update = SimpleNamespace(callback_query=query)
        context = SimpleNamespace(user_data={})

        state = await bot.leverage_button_handler(update, context)

        assert state == WAITING_RISK
        assert context.user_data[LEVERAGE_KEY] == 15
        assert query.answered
        text, kwargs, _ = query.message.replies[-1]
        assert "Плечо: 15x" in text
        assert [
            button.text
            for button in kwargs[
                "reply_markup"
            ].inline_keyboard[0]
        ] == ["1%", "2%", "3%"]

    asyncio.run(scenario())


def test_manual_leverage_button_waits_for_text_input():
    async def scenario():
        bot = make_bot()
        query = FakeQuery("leverage:manual")
        update = SimpleNamespace(callback_query=query)
        context = SimpleNamespace(user_data={})

        state = await bot.leverage_button_handler(update, context)

        assert state == WAITING_LEVERAGE
        assert LEVERAGE_KEY not in context.user_data
        assert "Введите плечо числом" in query.message.replies[-1][0]

    asyncio.run(scenario())


def test_risk_button_passes_only_selected_value_to_calculation():
    async def scenario():
        bot = make_bot()
        selected = {}

        async def calculate(
            message,
            progress_message,
            context,
            risk,
        ):
            selected["risk"] = risk
            selected["message"] = message
            selected["progress"] = progress_message
            return ConversationHandler.END

        bot._calculate_selected_risk = calculate
        query = FakeQuery("risk:3")
        update = SimpleNamespace(callback_query=query)
        context = SimpleNamespace(user_data={})

        state = await bot.risk_button_handler(update, context)

        assert state == ConversationHandler.END
        assert selected["risk"] == 3.0
        assert selected["message"] is query.message
        assert selected["progress"].text == "⏳ Считаю..."

    asyncio.run(scenario())
