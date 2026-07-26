from models.signal import OrderSide
from models.trade import PlannedTakeProfit, TradePlan
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
