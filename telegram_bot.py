import os
import asyncio
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler, CallbackQueryHandler
from core.api_client import BitunixClient
from services.account_service import AccountService
from services.signal_parser import SignalParser
from services.trade_service import TradeService
from config.settings import TradeSettings
from config.execution import ExecutionConfig
from models.trade import TradePlanningError, TradeProposal
from services.execution_service import ExecutionService
from services.order_service import OrderService
from services.proposal_service import (
    ProposalError,
    ProposalService,
)
from config.access import TelegramAccessConfig
from models.management import ManagementAction, ManagementProposal
from services.management_proposal_service import (
    ManagementProposalError,
    ManagementProposalService,
)
from services.position_service import PositionService

load_dotenv()

WAITING_LEVERAGE = 1
WAITING_RISK = 2
SIGNAL_KEY = "signal"
LEVERAGE_KEY = "leverage"
RISK_KEY = "risk"

class FuturesBot:
    def __init__(self):
        api_key = os.getenv("BITUNIX_API_KEY")
        api_secret = os.getenv("BITUNIX_API_SECRET")
        self.execution = ExecutionConfig.from_env(os.environ)
        self.access = TelegramAccessConfig.from_env(
            os.environ,
            self.execution.mode,
        )
        
        self.client = BitunixClient(
            api_key,
            api_secret,
            self.execution,
        )
        self.account_service = AccountService(self.client)
        self.order_service = OrderService(self.client)
        self.position_service = PositionService(self.client)
        self.execution_service = ExecutionService(
            self.order_service,
            self.account_service,
        )

    async def _authorize(self, update: Update) -> bool:
        user_id = update.effective_user.id if update.effective_user else None
        if self.access.is_allowed(user_id):
            return True
        if update.callback_query:
            await update.callback_query.answer(
                "Доступ запрещён",
                show_alert=True,
            )
        elif update.effective_message:
            await update.effective_message.reply_text("⛔ Доступ запрещён")
        return False
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._authorize(update):
            return ConversationHandler.END
        self._reset_user_state(context.user_data)
        await update.message.reply_text(
            "👋 Привет! Отправь мне сигнал из канала.\n"
            "Я распознаю его и помогу войти в сделку."
        )
    
    async def handle_signal(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._authorize(update):
            return ConversationHandler.END
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
        
        signal = SignalParser.parse(clean_message)
        
        if not signal:
            await update.message.reply_text("❌ Не удалось распознать сигнал.")
            return ConversationHandler.END

        self._reset_user_state(context.user_data)
        context.user_data[SIGNAL_KEY] = signal
        
        tp_lines = "\n".join(
            f"TP{i+1}: {tp}"
            for i, tp in enumerate(signal.take_profits)
        )
        
        await update.message.reply_text(
            f"✅ Сигнал распознан:\n\n"
            f"{signal.side.value} {signal.symbol}\n"
            f"Вход: {min(signal.entry_min, signal.entry_max)} - {max(signal.entry_min, signal.entry_max)}\n\n"
            f"{tp_lines}\n\n"
            f"SL: {signal.stop_loss}\n\n"
            f"Введите плечо (например, 10):"
        )
        return WAITING_LEVERAGE
    
    async def set_leverage(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._authorize(update):
            return ConversationHandler.END
        try:
            leverage = int(update.message.text)
            if leverage < 1 or leverage > 125:
                await update.message.reply_text("⚠️ Плечо от 1 до 125:")
                return WAITING_LEVERAGE
        except ValueError:
            await update.message.reply_text("⚠️ Введите число:")
            return WAITING_LEVERAGE

        context.user_data[LEVERAGE_KEY] = leverage
        
        await update.message.reply_text(
            f"✅ Плечо: {leverage}x\n"
            "Введите процент риска (например, 1):"
        )
        return WAITING_RISK
    
    async def set_risk(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._authorize(update):
            return ConversationHandler.END
        try:
            risk = float(update.message.text)
            if risk <= 0 or risk > 100:
                await update.message.reply_text("⚠️ Риск от 0.1 до 100:")
                return WAITING_RISK
        except ValueError:
            await update.message.reply_text("⚠️ Введите число:")
            return WAITING_RISK

        signal = context.user_data.get(SIGNAL_KEY)
        leverage = context.user_data.get(LEVERAGE_KEY)
        if signal is None or leverage is None:
            await update.message.reply_text(
                "❌ Сессия устарела. Отправьте сигнал заново."
            )
            return ConversationHandler.END

        context.user_data[RISK_KEY] = risk
        
        msg = await update.message.reply_text("⏳ Считаю...")
        
        try:
            settings = TradeSettings(
                leverage=leverage,
                risk_percent=risk,
            )
            trade_service = TradeService(self.client, settings)
            account = await asyncio.to_thread(
                self.account_service.get_account,
                "USDT",
            )
            plan = await asyncio.to_thread(
                trade_service.build_plan,
                signal,
                account,
            )
            proposal = TradeProposal.create(plan)
            ProposalService.store(context.user_data, proposal)
            order_info = plan.to_order_info()
            
            current = order_info["current_price"]
            total_qty = order_info["total_quantity"]
            sl = order_info["stop_loss"]
            take_profits = order_info["take_profits"]
            tp_quantities = order_info["tp_quantities"]
            risk_budget = order_info["risk_budget"]
            position_value = total_qty * current
            
            if signal.side.value == "LONG":
                sl_loss = (current - sl) * total_qty
            else:
                sl_loss = (sl - current) * total_qty
            
            text = f"📊 *РАСЧЁТ*\n\n"
            text += f"Режим: `{self.execution.mode.value}`\n"
            text += f"*{signal.side.value} {order_info['symbol']}*\n"
            text += f"Плечо: {leverage}x | Риск: {risk}%\n"
            text += f"Цена: {current}\n"
            text += f"Объём: {total_qty}\n"
            text += f"Позиция: {position_value:.2f} USDT\n"
            text += f"SL: {sl} (−{sl_loss:.2f} USDT)\n\n"
            text += (
                f"Риск-бюджет: {risk_budget:.2f} USDT\n"
                f"Расчётный риск: {sl_loss:.2f} USDT\n\n"
            )
            text += f"*Тейки:*\n"
            
            for i, (tp, qty) in enumerate(zip(take_profits, tp_quantities)):
                share = qty / total_qty * 100
                if signal.side.value == "LONG":
                    profit = (tp - current) * qty
                else:
                    profit = (current - tp) * qty
                text += f"TP{i+1}: {tp} | {qty} ({share:.0f}%) | +{profit:.2f} USDT\n"
            
            await msg.delete()
            await update.message.reply_text(text, parse_mode='Markdown')
            
            keyboard = [
                [
                    InlineKeyboardButton(
                        "✅ Войти",
                        callback_data=f"enter:{proposal.proposal_id}",
                    ),
                    InlineKeyboardButton(
                        "❌ Отмена",
                        callback_data=f"cancel:{proposal.proposal_id}",
                    ),
                ]
            ]
            await update.message.reply_text(
                "Подтвердите в течение 5 минут:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        except TradePlanningError as error:
            await msg.edit_text(f"❌ {error}")
        except Exception as e:
            await msg.edit_text(f"❌ Ошибка: {e}")
        
        return ConversationHandler.END
    
    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._authorize(update):
            return
        query = update.callback_query
        await query.answer()

        try:
            action, proposal_id = query.data.split(":", 1)
        except ValueError:
            await query.edit_message_text(
                "❌ Некорректная кнопка подтверждения"
            )
            return

        if action not in {"enter", "cancel"}:
            await query.edit_message_text("❌ Неизвестное действие")
            return

        try:
            proposal = ProposalService.consume(
                context.user_data,
                proposal_id,
            )
        except ProposalError as error:
            await query.edit_message_text(f"❌ {error}")
            return

        if action == "cancel":
            await query.edit_message_text("❌ Вход отменён")
            return

        await query.edit_message_text("⏳ Вхожу в сделку...")
        
        try:
            execution_result = await asyncio.to_thread(
                self.execution_service.execute,
                proposal.plan,
            )
            result = execution_result.to_dict()
            
            if result["success"]:
                if result["simulated"]:
                    msg = (
                        "🧪 *Симуляция завершена*\n"
                        "Ордера не отправлялись на Bitunix.\n\n"
                    )
                else:
                    msg = "✅ *Сделка открыта!*\n\n"
                for o in result["orders"]:
                    msg += f"TP{o['tp']} @ {o['price']}: {o['qty']}\n"
                msg += f"\nSL: {result['stop_loss']}"
                await query.edit_message_text(msg, parse_mode='Markdown')
            else:
                if result.get("partial"):
                    partial_text = (
                        "⚠️ *Позиция открыта частично*\n"
                        "Дальнейшие заявки остановлены.\n\n"
                    )
                    for order in result["orders"]:
                        partial_text += (
                            f"TP{order['tp']}: "
                            f"{order['qty']} "
                            f"(ID {order['id']})\n"
                        )
                    partial_text += f"\n❌ {result['error']}"
                    await query.edit_message_text(
                        partial_text,
                        parse_mode="Markdown",
                    )
                else:
                    await query.edit_message_text(
                        f"❌ {result['error']}"
                    )
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка: {e}")

    async def mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._authorize(update):
            return
        await update.message.reply_text(
            f"🛡️ Режим исполнения: {self.execution.mode.value}"
        )

    async def positions(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        if not await self._authorize(update):
            return
        try:
            positions = await asyncio.to_thread(
                self.position_service.get_open_positions
            )
            if not positions:
                await update.message.reply_text("Открытых позиций нет")
                return
            lines = ["Открытые позиции:"]
            for position in positions:
                lines.append(
                    f"{position.position_id}: {position.side} "
                    f"{position.symbol}, qty={position.quantity}, "
                    f"entry={position.average_open_price}, "
                    f"PnL={position.unrealized_pnl}"
                )
            await update.message.reply_text("\n".join(lines))
        except Exception as error:
            await update.message.reply_text(f"❌ Ошибка: {error}")

    async def orders(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        if not await self._authorize(update):
            return
        try:
            orders = await asyncio.to_thread(
                self.order_service.get_pending_orders
            )
            if not orders:
                await update.message.reply_text("Открытых ордеров нет")
                return
            lines = ["Открытые ордера:"]
            for order in orders:
                lines.append(
                    f"{order.order_id}: {order.side} {order.symbol}, "
                    f"qty={order.quantity}, price={order.price}, "
                    f"status={order.status}"
                )
            await update.message.reply_text("\n".join(lines))
        except Exception as error:
            await update.message.reply_text(f"❌ Ошибка: {error}")

    async def cancel_order(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        if not await self._authorize(update):
            return
        if len(context.args) != 2:
            await update.message.reply_text(
                "Использование: /cancel_order SYMBOL ORDER_ID"
            )
            return
        symbol, order_id = context.args
        proposal = ManagementProposal.create(
            ManagementAction.CANCEL_ORDER,
            order_id,
            symbol=symbol.upper(),
        )
        ManagementProposalService.store(context.user_data, proposal)
        await self._send_management_confirmation(
            update,
            proposal,
            f"Отменить ордер {order_id} ({symbol.upper()})?",
        )

    async def close_position(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        if not await self._authorize(update):
            return
        if len(context.args) != 1:
            await update.message.reply_text(
                "Использование: /close_position POSITION_ID"
            )
            return
        proposal = ManagementProposal.create(
            ManagementAction.CLOSE_POSITION,
            context.args[0],
        )
        ManagementProposalService.store(context.user_data, proposal)
        await self._send_management_confirmation(
            update,
            proposal,
            f"Закрыть позицию {proposal.target_id}?",
        )

    async def _send_management_confirmation(
        self,
        update: Update,
        proposal: ManagementProposal,
        text: str,
    ) -> None:
        keyboard = [[
            InlineKeyboardButton(
                "✅ Подтвердить",
                callback_data=f"manage:confirm:{proposal.proposal_id}",
            ),
            InlineKeyboardButton(
                "❌ Отмена",
                callback_data=f"manage:cancel:{proposal.proposal_id}",
            ),
        ]]
        await update.message.reply_text(
            f"{text}\nПодтвердите в течение 5 минут.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def management_button_handler(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        if not await self._authorize(update):
            return
        query = update.callback_query
        await query.answer()
        try:
            prefix, decision, proposal_id = query.data.split(":", 2)
            if prefix != "manage" or decision not in {"confirm", "cancel"}:
                raise ValueError
        except ValueError:
            await query.edit_message_text("❌ Некорректная кнопка")
            return
        try:
            proposal = ManagementProposalService.consume(
                context.user_data,
                proposal_id,
            )
        except ManagementProposalError as error:
            await query.edit_message_text(f"❌ {error}")
            return
        if decision == "cancel":
            await query.edit_message_text("❌ Операция отменена")
            return

        await query.edit_message_text("⏳ Выполняю...")
        try:
            if proposal.action is ManagementAction.CANCEL_ORDER:
                result = await asyncio.to_thread(
                    self.order_service.cancel_orders,
                    proposal.symbol,
                    (proposal.target_id,),
                )
                status = (
                    "симулирована"
                    if result.simulated
                    else "отправлена; финальный статус ожидается"
                )
                await query.edit_message_text(
                    f"✅ Отмена ордера {status}"
                )
            else:
                result = await asyncio.to_thread(
                    self.position_service.close_position,
                    proposal.target_id,
                )
                status = (
                    "симулировано"
                    if result.simulated
                    else "отправлено; финальный статус ожидается"
                )
                await query.edit_message_text(
                    f"✅ Закрытие позиции {status}"
                )
        except Exception as error:
            await query.edit_message_text(f"❌ Ошибка: {error}")

    @staticmethod
    def _reset_user_state(user_data):
        user_data.pop(SIGNAL_KEY, None)
        user_data.pop(LEVERAGE_KEY, None)
        user_data.pop(RISK_KEY, None)
        ProposalService.discard(user_data)


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
    app.add_handler(CommandHandler("mode", bot.mode))
    app.add_handler(CommandHandler("positions", bot.positions))
    app.add_handler(CommandHandler("orders", bot.orders))
    app.add_handler(CommandHandler("cancel_order", bot.cancel_order))
    app.add_handler(CommandHandler("close_position", bot.close_position))
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(
        bot.management_button_handler,
        pattern=r"^manage:",
    ))
    app.add_handler(CallbackQueryHandler(
        bot.button_handler,
        pattern=r"^(enter|cancel):",
    ))
    
    print("🤖 Бот запущен...")
    print(f"🛡️ Режим исполнения: {bot.execution.mode.value}")
    app.run_polling()

if __name__ == "__main__":
    main()
