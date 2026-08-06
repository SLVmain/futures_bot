import asyncio
from types import SimpleNamespace

from telegram.ext import ConversationHandler

from config.execution import ExecutionMode
from models.signal import OrderSide, TradeSignal
from models.trade import PlannedTakeProfit, TradePlan, TradeProposal
from telegram_bot import (
    LEVERAGE_KEY,
    RISK_KEY,
    SIGNAL_KEY,
    WAITING_LEVERAGE,
    WAITING_RISK,
    WAITING_TP_STRATEGY,
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
        self.deleted = False

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))

    async def delete(self):
        self.deleted = True


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


def make_signal(tp_count):
    return TradeSignal(
        symbol="BTCUSDT",
        side=OrderSide.LONG,
        entry_min=100,
        entry_max=101,
        take_profits=list(range(102, 102 + tp_count)),
        stop_loss=95,
    )


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


def test_risk_button_stores_value_and_shows_five_tp_strategies():
    async def scenario():
        bot = make_bot()
        query = FakeQuery("risk:3")
        update = SimpleNamespace(callback_query=query)
        context = SimpleNamespace(user_data={
            SIGNAL_KEY: make_signal(5),
            LEVERAGE_KEY: 15,
        })

        state = await bot.risk_button_handler(update, context)

        assert state == WAITING_TP_STRATEGY
        assert context.user_data[RISK_KEY] == 3.0
        text, kwargs, _ = query.message.replies[-1]
        assert "Сколько тейков использовать?" in text
        assert [
            row[0].callback_data
            for row in kwargs["reply_markup"].inline_keyboard
        ] == [
            "tp_strategy:5",
            "tp_strategy:3",
            "tp_strategy:1",
        ]

    asyncio.run(scenario())


def test_three_tp_signal_offers_first_or_all_three():
    keyboard = FuturesBot._tp_strategy_keyboard(
        make_signal(3)
    )

    assert [
        row[0].callback_data
        for row in keyboard.inline_keyboard
    ] == ["tp_strategy:3", "tp_strategy:1"]


def test_tp_strategy_uses_first_three_targets_before_calculation():
    async def scenario():
        bot = make_bot()
        selected = {}

        async def calculate(
            message,
            progress_message,
            context,
            risk,
        ):
            selected["signal"] = context.user_data[SIGNAL_KEY]
            selected["risk"] = risk
            return ConversationHandler.END

        bot._calculate_selected_risk = calculate
        query = FakeQuery("tp_strategy:3")
        update = SimpleNamespace(callback_query=query)
        context = SimpleNamespace(user_data={
            SIGNAL_KEY: make_signal(5),
            LEVERAGE_KEY: 20,
            RISK_KEY: 2.0,
        })

        state = await bot.tp_strategy_button_handler(
            update,
            context,
        )

        assert state == ConversationHandler.END
        assert selected["risk"] == 2.0
        assert selected["signal"].take_profits == [102, 103, 104]
        assert context.user_data[SIGNAL_KEY].take_profits == [
            102,
            103,
            104,
        ]

    asyncio.run(scenario())


def test_single_tp_strategy_keeps_only_first_target():
    signal = make_signal(3)

    assert FuturesBot._tp_strategy_options(signal) == (3, 1)
    assert signal.take_profits[:1] == [102]


def test_api_unsupported_plan_is_shown_without_entry_button(
    monkeypatch,
):
    async def run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(
        "telegram_bot.asyncio.to_thread",
        run_inline,
    )

    async def scenario():
        bot = make_bot()
        plan = TradePlan(
            symbol="BTCUSDT",
            side=OrderSide.LONG,
            entry_min=49,
            entry_max=51,
            current_price=50,
            in_range=True,
            total_quantity=2,
            stop_loss=45,
            take_profits=(PlannedTakeProfit(55, 2),),
            leverage=10,
            risk_percent=1,
            risk_budget=10,
            api_execution_supported=False,
            trigger_price=52,
        )

        class ManualTradeService:
            def __init__(self, client, settings):
                pass

            def build_plan(self, signal, account):
                return plan

        monkeypatch.setattr(
            "telegram_bot.TradeService",
            ManualTradeService,
        )
        bot.client = object()
        bot.execution = SimpleNamespace(mode=ExecutionMode.LIVE)
        bot.account_service = SimpleNamespace(
            get_account=lambda coin: SimpleNamespace()
        )
        bot.order_service = SimpleNamespace(
            get_pending_orders=lambda symbol: ()
        )
        bot.position_service = SimpleNamespace(
            get_open_positions=lambda symbol: ()
        )
        progress = FakeProgressMessage("Считаю")

        class MessageBeforeProgressDeletion(FakeMessage):
            async def reply_text(self, text, **kwargs):
                if not self.replies:
                    assert progress.deleted is False
                return await super().reply_text(text, **kwargs)

        message = MessageBeforeProgressDeletion()
        stale = TradeProposal.create(plan)
        user_data = {"trade_proposal": stale}

        await bot._calculate_and_send_proposal(
            message,
            progress,
            user_data,
            make_signal(1),
            10,
            1,
        )

        assert progress.deleted is True
        assert "РУЧНОЕ РАЗМЕЩЕНИЕ" in message.replies[0][0]
        assert "Тип ордера: TRIGGER\\_LIMIT" in (
            message.replies[0][0]
        )
        assert "Кнопка автоматического входа отключена" in (
            message.replies[1][0]
        )
        assert "reply_markup" not in message.replies[1][1]
        assert "trade_proposal" not in user_data

    asyncio.run(scenario())
