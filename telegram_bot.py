import os
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler, CallbackQueryHandler
from core.api_client import BitunixClient
from services.account_service import AccountService
from services.signal_parser import SignalParser
from services.trade_service import TradeService
from config.settings import TradeSettings

load_dotenv()

WAITING_LEVERAGE = 1
WAITING_RISK = 2

class FuturesBot:
    def __init__(self):
        api_key = os.getenv("BITUNIX_API_KEY")
        api_secret = os.getenv("BITUNIX_API_SECRET")
        
        self.client = BitunixClient(api_key, api_secret)
        self.account_service = AccountService(self.client)
        self.signal = None
        self.leverage = 10
        self.risk = 1.0
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "👋 Привет! Отправь мне сигнал из канала.\n"
            "Я распознаю его и помогу войти в сделку."
        )
    
    async def handle_signal(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        message = update.message.text or update.message.caption
        
        if not message:
            await update.message.reply_text("❌ Не удалось прочитать сообщение.")
            return ConversationHandler.END
        
        lines = message.split('\n')
        clean_lines = []
        skip = True
        for line in lines:
            if skip and (line.startswith('Forwarded from') or line.startswith('Автор:') or line.startswith('Author:')):
                continue
            if line.strip().startswith('#'):
                skip = False
            if not skip:
                clean_lines.append(line)
        
        clean_message = '\n'.join(clean_lines).strip()
        
        if not clean_message or '#' not in clean_message:
            clean_message = message
        
        print(f"📨 Очищенное сообщение:\n{clean_message[:300]}")
        
        self.signal = SignalParser.parse(clean_message)
        
        if not self.signal:
            await update.message.reply_text("❌ Не удалось распознать сигнал.")
            return ConversationHandler.END
        
        tp_lines = "\n".join([f"TP{i+1}: {tp}" for i, tp in enumerate(self.signal.take_profits)])
        
        await update.message.reply_text(
            f"✅ Сигнал распознан:\n\n"
            f"{self.signal.side.value} {self.signal.symbol}\n"
            f"Вход: {min(self.signal.entry_min, self.signal.entry_max)} - {max(self.signal.entry_min, self.signal.entry_max)}\n\n"
            f"{tp_lines}\n\n"
            f"SL: {self.signal.stop_loss}\n\n"
            f"Введите плечо (например, 10):"
        )
        return WAITING_LEVERAGE
    
    async def set_leverage(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            self.leverage = int(update.message.text)
            if self.leverage < 1 or self.leverage > 125:
                await update.message.reply_text("⚠️ Плечо от 1 до 125:")
                return WAITING_LEVERAGE
        except ValueError:
            await update.message.reply_text("⚠️ Введите число:")
            return WAITING_LEVERAGE
        
        await update.message.reply_text(f"✅ Плечо: {self.leverage}x\nВведите процент риска (например, 1):")
        return WAITING_RISK
    
    async def set_risk(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            self.risk = float(update.message.text)
            if self.risk <= 0 or self.risk > 100:
                await update.message.reply_text("⚠️ Риск от 0.1 до 100:")
                return WAITING_RISK
        except ValueError:
            await update.message.reply_text("⚠️ Введите число:")
            return WAITING_RISK
        
        msg = await update.message.reply_text("⏳ Считаю...")
        
        try:
            settings = TradeSettings(leverage=self.leverage, risk_percent=self.risk)
            trade_service = TradeService(self.client, settings)
            account = self.account_service.get_account("USDT")
            
            if account.get("code") != 0:
                await msg.edit_text("❌ Ошибка аккаунта")
                return ConversationHandler.END
            
            account_data = account["data"]
            order_info = trade_service.prepare_order(self.signal, account_data)
            
            if not order_info["ready"]:
                await msg.edit_text(f"❌ {order_info['reason']}")
                return ConversationHandler.END
            
            current = order_info["current_price"]
            total_qty = order_info["total_quantity"]
            sl = order_info["stop_loss"]
            take_profits = order_info["take_profits"]
            tp_quantities = order_info["tp_quantities"]
            position_value = total_qty * current
            
            if self.signal.side.value == "LONG":
                sl_loss = (current - sl) * total_qty
            else:
                sl_loss = (sl - current) * total_qty
            
            text = f"📊 *РАСЧЁТ*\n\n"
            text += f"*{self.signal.side.value} {order_info['symbol']}*\n"
            text += f"Плечо: {self.leverage}x | Риск: {self.risk}%\n"
            text += f"Цена: {current}\n"
            text += f"Объём: {total_qty}\n"
            text += f"Позиция: {position_value:.2f} USDT\n"
            text += f"SL: {sl} (−{sl_loss:.2f} USDT)\n\n"
            text += f"*Тейки:*\n"
            
            for i, (tp, qty) in enumerate(zip(take_profits, tp_quantities)):
                share = qty / total_qty * 100
                if self.signal.side.value == "LONG":
                    profit = (tp - current) * qty
                else:
                    profit = (current - tp) * qty
                text += f"TP{i+1}: {tp} | {qty} ({share:.0f}%) | +{profit:.2f} USDT\n"
            
            await msg.delete()
            await update.message.reply_text(text, parse_mode='Markdown')
            
            keyboard = [
                [
                    InlineKeyboardButton("✅ Войти", callback_data="enter"),
                    InlineKeyboardButton("❌ Отмена", callback_data="cancel"),
                ]
            ]
            await update.message.reply_text("Подтвердите:", reply_markup=InlineKeyboardMarkup(keyboard))
        
        except Exception as e:
            await msg.edit_text(f"❌ Ошибка: {e}")
        
        return ConversationHandler.END
    
    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        if query.data == "cancel":
            await query.edit_message_text("❌ Вход отменён")
            return
        
        await query.edit_message_text("⏳ Вхожу в сделку...")
        
        try:
            settings = TradeSettings(leverage=self.leverage, risk_percent=self.risk)
            trade_service = TradeService(self.client, settings)
            account = self.account_service.get_account("USDT")
            
            result = trade_service.enter_position(self.signal, account["data"])
            
            if result["success"]:
                msg = "✅ *Сделка открыта!*\n\n"
                for o in result["orders"]:
                    msg += f"TP{o['tp']} @ {o['price']}: {o['qty']}\n"
                msg += f"\nSL: {result['stop_loss']}"
                await query.edit_message_text(msg, parse_mode='Markdown')
            else:
                await query.edit_message_text(f"❌ {result['error']}")
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка: {e}")


def main():
    bot = FuturesBot()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    proxy_url = os.getenv("TELEGRAM_PROXY", None)
    
    builder = Application.builder().token(token)
    if proxy_url:
        builder = builder.proxy_url(proxy_url)
    else:
        builder = builder.connect_timeout(30).read_timeout(30).write_timeout(30)
    
    app = builder.build()
    
    conv_handler = ConversationHandler(
        entry_points=[MessageHandler((filters.TEXT | filters.PHOTO | filters.CAPTION) & ~filters.COMMAND, bot.handle_signal)],
        states={
            WAITING_LEVERAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.set_leverage)],
            WAITING_RISK: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.set_risk)],
        },
        fallbacks=[CommandHandler("start", bot.start)],
    )
    
    app.add_handler(CommandHandler("start", bot.start))
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(bot.button_handler))
    
    print("🤖 Бот запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()