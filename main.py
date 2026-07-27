from dotenv import load_dotenv
import os
from core.api_client import BitunixClient
from services.account_service import AccountService
from services.signal_parser import SignalParser
from services.trade_service import TradeService
from config.settings import TradeSettings
from config.execution import ExecutionConfig, ExecutionMode
from models.trade import TradePlanningError

load_dotenv()

def main():
    api_key = os.getenv("BITUNIX_API_KEY")
    api_secret = os.getenv("BITUNIX_API_SECRET")
    
    # НАСТРОЙКИ
    settings = TradeSettings(
        leverage=1,        # плечо
        risk_percent=1.0,   # % риска от депозита
        max_tp_count=3,     # сколько тейков использовать
        tp1_share=0.5,      # 50% на первый тейк
        tp2_share=0.25,     # 25% на второй
        tp3_share=0.25,     # 25% на третий
    )
    
    execution = ExecutionConfig.from_env(os.environ)
    client = BitunixClient(api_key, api_secret, execution)
    account_service = AccountService(client)
    trade_service = TradeService(client, settings)

    print(f"🛡️ Режим исполнения: {execution.mode.value}")
    if execution.mode is ExecutionMode.LIVE:
        print("🚨 LIVE: ПОДТВЕРЖДЁННЫЕ СДЕЛКИ БУДУТ РЕАЛЬНЫМИ")
    
    test_message = """#ASTERUSDT.P ПРОДАЖА

Диапазон входа:
0.62075-0.61965

Тейк-профит 1: 0.618
Тейк-профит 2: 0.6169
Тейк-профит 3: 0.6147
Тейк-профит 4: 0.6103
Тейк-профит 5: 0.6015

⚠️ Стоп-лосс: 0.6257"""
    
    signal = SignalParser.parse(test_message)
    
    if not signal:
        print("❌ Не удалось распарсить сигнал")
        return
    
    print("✅ Сигнал распознан:")
    print(signal)
    
    account = account_service.get_account("USDT")
    balance = float(account.available)
    print(f"\n💰 Баланс: {balance} USDT")
    
    print("\n📊 Проверка цены...")
    try:
        plan = trade_service.build_plan(signal, account)
    except TradePlanningError as error:
        print(f"❌ Не готово: {error}")
        return

    order_info = plan.to_order_info()
    
    if order_info["ready"]:
        current = order_info["current_price"]
        entry_min = order_info["entry_min"]
        entry_max = order_info["entry_max"]
        mid = (entry_min + entry_max) / 2
        total_qty = order_info["total_quantity"]
        sl = order_info["stop_loss"]
        take_profits = order_info["take_profits"]
        tp_quantities = order_info["tp_quantities"]
        risk_budget = order_info["risk_budget"]
        
        position_value = total_qty * current
        risk_pct = (position_value / balance) * 100
        
        # Расчёт SL
        if signal.side.value == "LONG":
            sl_loss = (current - sl) * total_qty
        else:
            sl_loss = (sl - current) * total_qty
        
        sl_loss_pct = (sl_loss / balance) * 100
        
        print(f"\n{'='*50}")
        print(f"📋 ГОТОВО К ОТКРЫТИЮ ПОЗИЦИИ:")
        print(f"   Символ: {order_info['symbol']}")
        print(f"   Сторона: {order_info['side']}")
        print(f"   Плечо: {settings.leverage}x | Риск: {settings.risk_percent}%")
        print(f"   Текущая цена: {current}")
        print(f"   Диапазон входа: {entry_min} - {entry_max}")
        print(f"   Отклонение: {((current - mid) / mid * 100):.2f}%")
        print(f"   В диапазоне: {'✅ ДА' if order_info['in_range'] else '❌ НЕТ'}")
        print(f"   Общий объём: {total_qty}")
        print(f"   Позиция: {position_value:.2f} USDT ({risk_pct:.1f}% от депозита)")
        print(f"   Стоп-лосс: {sl} (потеря: {sl_loss:.2f} USDT / {sl_loss_pct:.2f}%)")
        print(
            f"   Риск-бюджет: {risk_budget:.2f} USDT | "
            f"Расчётный риск: {sl_loss:.2f} USDT"
        )
        print(f"\n📊 ТЕЙК-ПРОФИТЫ:")
        
        for i, (tp, qty) in enumerate(zip(take_profits, tp_quantities)):
            if signal.side.value == "LONG":
                tp_profit = (tp - current) * qty
            else:
                tp_profit = (current - tp) * qty
            share = qty / total_qty * 100
            print(f"   TP{i+1}: {tp} | Объём: {qty} ({share:.0f}%) | Прибыль: {tp_profit:.2f} USDT")
        
        print(f"{'='*50}")
        
        if order_info["in_range"]:
            choice = input("\n🔔 Цена в диапазоне! Войти в сделку? (y/n): ").lower()
            if choice == 'y':
                result = trade_service.execute_plan(plan).to_dict()
                if result["success"]:
                    if result["simulated"]:
                        print(
                            "\n🧪 Симуляция завершена. "
                            "Ордера не отправлялись на Bitunix."
                        )
                    else:
                        print(f"\n✅ Сделка открыта!")
                    for o in result["orders"]:
                        print(f"   TP{o['tp']} @ {o['price']}: {o['qty']} | ID: {o['id']}")
                else:
                    if result.get("partial"):
                        print(
                            "⚠️ Позиция открыта частично. "
                            "Автоматическое продолжение остановлено."
                        )
                        for order in result["orders"]:
                            print(
                                f"   TP{order['tp']} "
                                f"ID: {order['id']}"
                            )
                    print(f"❌ Ошибка входа: {result['error']}")
            else:
                print("❌ Вход отменён")
        else:
            if current < entry_min:
                print(f"\n📉 Цена НИЖЕ диапазона. Ждём роста до {entry_min}")
            else:
                print(f"\n📈 Цена ВЫШЕ диапазона. Ждём снижения до {entry_max}")
if __name__ == "__main__":
    main()
