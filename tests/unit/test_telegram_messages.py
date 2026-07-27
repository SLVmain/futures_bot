import asyncio

from models.signal import OrderSide
from models.trade import PlannedTakeProfit, TradePlan
from services.trade_journal import CsvTradeJournal, JournalEvent
from telegram_bot import FuturesBot


def test_limit_order_result_is_explicitly_formatted():
    plan = TradePlan(
        symbol="TESTUSDT",
        side=OrderSide.LONG,
        entry_min=0.015,
        entry_max=0.016,
        current_price=0.017,
        in_range=False,
        total_quantity=13356,
        stop_loss=0.014,
        take_profits=(
            PlannedTakeProfit(0.01655, 13356),
        ),
        leverage=10,
        risk_percent=1,
        risk_budget=10,
        limit_price=0.0155,
    )
    result = {
        "simulated": False,
        "orders": [{
            "tp": 1,
            "price": 0.01655,
            "qty": 13356,
        }],
        "stop_loss": 0.014,
    }

    message = FuturesBot._format_success_message(plan, result)

    assert "Лимитный ордер отправлен" in message
    assert "Вход: 0.0155" in message
    assert "Объём: 13356" in message
    assert "TP1: 0.01655 — 13356 (100%)" in message
    assert "SL: 0.014" in message
    assert "Статус входа: ожидает исполнения" in message
    assert "TP будут добавлены после исполнения" in message


class FakeMessage:
    def __init__(self):
        self.texts = []
        self.documents = []

    async def reply_text(self, text, **kwargs):
        self.texts.append(text)

    async def reply_document(self, **kwargs):
        self.documents.append(kwargs)


class FakeUpdate:
    def __init__(self):
        self.message = FakeMessage()


def make_reporting_bot(tmp_path):
    bot = FuturesBot.__new__(FuturesBot)
    bot.journal = CsvTradeJournal(tmp_path / "journal.csv")

    async def authorize(update):
        return True

    bot._authorize = authorize
    return bot


def test_telegram_trade_reports_and_export(tmp_path):
    async def scenario():
        bot = make_reporting_bot(tmp_path)
        bot.journal.append(JournalEvent(
            event_type="TRADE_SUMMARY",
            status="CLOSED",
            symbol="BTCUSDT",
            side="LONG",
            pnl="5",
            fee="0.2",
            funding="-0.1",
            net_pnl="4.7",
            source_event_id="summary-1",
        ))

        trades_update = FakeUpdate()
        stats_update = FakeUpdate()
        export_update = FakeUpdate()

        await bot.trades(trades_update, None)
        await bot.stats(stats_update, None)
        await bot.export_journal(export_update, None)

        assert "BTCUSDT LONG" in trades_update.message.texts[0]
        assert "net=4.7" in trades_update.message.texts[0]
        assert "Всего: 1" in stats_update.message.texts[0]
        assert "Win rate: 100.00%" in stats_update.message.texts[0]
        assert "Чистый PnL: 4.7" in stats_update.message.texts[0]
        exported = export_update.message.documents[0]
        assert exported["filename"] == "trade_journal.csv"
        assert b"TRADE_SUMMARY" in exported["document"].getvalue()

    asyncio.run(scenario())
