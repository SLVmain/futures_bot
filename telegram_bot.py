import os
import asyncio
import hashlib
import logging
import re
import sys
import time
from dataclasses import replace
from io import BytesIO
from dotenv import load_dotenv
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import NetworkError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from core.api_client import BitunixClient
from services.account_service import AccountService
from services.signal_parser import SignalParser
from services.signal_update_parser import SignalUpdateParser
from services.trade_service import TradeService
from config.settings import TradeSettings
from config.execution import ExecutionConfig, ExecutionMode
from models.trade import TradePlanningError, TradeProposal
from services.execution_service import ExecutionService
from services.order_service import OrderService
from services.proposal_service import (
    ProposalError,
    ProposalService,
)
from config.access import TelegramAccessConfig
from config.monitoring import MonitoringConfig
from config.triggers import TriggerConfig
from models.management import ManagementAction, ManagementProposal
from models.signal_update import SignalUpdate, SignalUpdateType
from services.management_proposal_service import (
    ManagementProposalError,
    ManagementProposalService,
)
from services.position_service import PositionService
from services.position_monitor import PositionMonitor
from services.private_websocket import BitunixPrivateWebSocket
from services.protection_service import ProtectionService
from services.trade_journal import CsvTradeJournal, JournalEvent
from services.market_service import MarketService
from services.emulated_trigger_service import (
    EmulatedTriggerService,
    EmulatedTriggerStore,
)
from models.signal import TradeSignal
from services.trade_planner import TradePlanner

load_dotenv()

WAITING_LEVERAGE = 1
WAITING_RISK = 2
WAITING_TP_STRATEGY = 3
SIGNAL_KEY = "signal"
LEVERAGE_KEY = "leverage"
RISK_KEY = "risk"
LOGGER = logging.getLogger(__name__)
SIGNAL_UPDATE_PATTERN = re.compile(
    r"(?:\bотмена\b|идея\s+закрыт|идея\s+закрыва|"
    r"достигнут[ао]?\s+первая\s+цель|"
    r"первая\s+цель\s+достигнут|"
    r"закрыт[ао]?\s+из-за\s+противоположн)",
    re.IGNORECASE,
)


async def telegram_error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    error = context.error
    if isinstance(error, NetworkError):
        LOGGER.warning(
            "Telegram временно недоступен: %s. "
            "Повторное подключение выполняется автоматически.",
            type(error).__name__,
        )
        return
    LOGGER.error(
        "Необработанная ошибка Telegram: %s",
        type(error).__name__,
        exc_info=(
            type(error),
            error,
            error.__traceback__,
        ),
    )

class FuturesBot:
    def __init__(self):
        api_key = os.getenv("BITUNIX_API_KEY")
        api_secret = os.getenv("BITUNIX_API_SECRET")
        self.execution = ExecutionConfig.from_env(os.environ)
        self.access = TelegramAccessConfig.from_env(
            os.environ,
            self.execution.mode,
        )
        self.monitoring = MonitoringConfig.from_env(
            os.environ,
            self.execution.mode,
        )
        self.triggers = TriggerConfig.from_env(os.environ)
        
        self.client = BitunixClient(
            api_key,
            api_secret,
            self.execution,
        )
        self.account_service = AccountService(self.client)
        self.order_service = OrderService(self.client)
        self.position_service = PositionService(self.client)
        self.market_service = MarketService(self.client)
        self.protection_service = ProtectionService(self.client)
        self.journal = CsvTradeJournal(
            self.monitoring.journal_path
        )
        self.monitor = None
        self.websocket = None
        self.websocket_task = None
        self.trigger_service = None
        self.execution_service = ExecutionService(
            self.order_service,
            self.account_service,
        )
        self._recent_signal_updates: dict[str, float] = {}

    async def post_init(self, application: Application) -> None:
        await application.bot.set_my_commands([
            BotCommand("start", "Начать работу и отправить сигнал"),
            BotCommand("help", "Подсказка по командам"),
            BotCommand("mode", "Показать режим торговли"),
            BotCommand("positions", "Открытые позиции"),
            BotCommand("orders", "Активные ордера"),
            BotCommand("trades", "Последние сделки журнала"),
            BotCommand("stats", "Статистика торговли"),
            BotCommand("export", "Скачать журнал сделок"),
            BotCommand("cancel_order", "Отменить ордер"),
            BotCommand("close_position", "Закрыть позицию"),
            BotCommand("ping", "Проверить, отвечает ли бот"),
            BotCommand("restart", "Перезапустить бот"),
        ])
        if self.execution.mode is ExecutionMode.LIVE:
            for chat_id in self.access.allowed_user_ids:
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "🚨 ВНИМАНИЕ: БОТ ЗАПУЩЕН В LIVE-РЕЖИМЕ\n"
                        "Подтверждённые сделки создают реальные "
                        "ордера на Bitunix."
                    ),
                )
        async def notify(
            text: str,
            action_id: str | None = None,
        ) -> None:
            reply_markup = None
            if action_id is not None:
                reply_markup = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "✅ Перенести SL",
                        callback_data=(
                            f"breakeven:confirm:{action_id}"
                        ),
                    ),
                    InlineKeyboardButton(
                        "❌ Оставить как есть",
                        callback_data=(
                            f"breakeven:cancel:{action_id}"
                        ),
                    ),
                ]])
            for chat_id in self.access.allowed_user_ids:
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=reply_markup,
                )
        if self.monitoring.enabled:
            api_key = self.client.sig_gen.api_key
            api_secret = self.client.sig_gen.api_secret
            if not api_key or not api_secret:
                raise ValueError(
                    "Bitunix credentials are required for monitoring"
                )
            self.monitor = PositionMonitor(
                self.order_service,
                self.position_service,
                self.protection_service,
                self.journal,
                notify,
                self.market_service,
                self.monitoring.taker_fee_rate,
                auto_break_even_on_tp1=(
                    self.monitoring.auto_break_even_on_tp1
                ),
            )
            self.websocket = BitunixPrivateWebSocket(
                api_key,
                api_secret,
                self.monitoring.websocket_url,
                self.monitor.handle_event,
                connected_handler=self.monitor.reconcile,
                status_handler=notify,
            )
            self.websocket_task = asyncio.create_task(
                self.websocket.run(),
                name="bitunix-private-websocket",
            )

        if self.triggers.enabled:
            async def register_execution(plan, result) -> None:
                await asyncio.to_thread(
                    self.journal.append,
                    JournalEvent(
                        event_type="execution",
                        status=result.status,
                        symbol=plan.symbol,
                        side=plan.side.value,
                        order_type=plan.order_type,
                        entry_price=str(plan.current_price),
                        quantity=str(plan.total_quantity),
                        leverage=str(plan.leverage),
                        risk_percent=str(plan.risk_percent),
                        stop_loss=str(plan.stop_loss),
                        take_profit=str(plan.take_profits[0].price),
                        execution_id=plan.execution_id,
                        mode=self.execution.mode.value,
                        order_id=(
                            result.orders[0].order_id
                            if result.orders else ""
                        ),
                        simulated=str(result.simulated).lower(),
                        error=result.error or "",
                        source_event_id=(
                            f"trigger:{plan.execution_id}:execution"
                        ),
                    ),
                )
                if self.monitor is None:
                    return
                client_ids = self.execution_service.client_ids(plan)
                for order in result.orders:
                    self.monitor.register_plan(
                        client_ids[order.tp_number - 1],
                        plan,
                        tp_number=order.tp_number,
                    )

            self.trigger_service = EmulatedTriggerService(
                self.market_service,
                self.execution_service,
                self.order_service,
                self.position_service,
                EmulatedTriggerStore(self.triggers.state_path),
                notify,
                register_execution,
                poll_interval=self.triggers.poll_interval,
                max_age_seconds=self.triggers.max_age_seconds,
            )
            suspended = await self.trigger_service.start()
            for record in suspended:
                await self._send_trigger_recovery(application, record)

    async def post_shutdown(self, application: Application) -> None:
        if self.trigger_service is not None:
            await self.trigger_service.stop()
        if self.websocket is not None:
            await self.websocket.stop()
        if self.websocket_task is not None:
            self.websocket_task.cancel()
            await asyncio.gather(
                self.websocket_task,
                return_exceptions=True,
            )

    async def _send_trigger_recovery(self, application, record) -> None:
        price = await self.trigger_service.price(record)
        plan = record.plan
        condition = (
            f"LONG: последняя цена ≥ {plan.trigger_price}"
            if plan.side.value == "LONG"
            else f"SHORT: последняя цена ≤ {plan.trigger_price}"
        )
        age_minutes = max(0, int((time.time() - record.created_at) / 60))
        tp_text = ", ".join(
            f"TP{i} {tp.price} × {tp.quantity}"
            for i, tp in enumerate(plan.take_profits, 1)
        )
        text = (
            f"⚠️ Найден приостановленный триггер "
            f"{plan.side.value} {plan.symbol}\n\n"
            "Во время остановки бота цена могла пересечь уровень.\n"
            "Автоматический вход не выполнен.\n\n"
            f"Создан: {age_minutes} мин. назад\n"
            f"Условие: {condition}\n"
            f"Триггер: {plan.trigger_price}\n"
            f"Диапазон сигнала: {plan.entry_min}–{plan.entry_max}\n"
            f"Цена перед остановкой: {record.last_price or 'нет данных'}\n"
            f"Текущая цена Bitunix: {price or 'недоступна'}\n"
            f"После триггера: MARKET, объём {plan.total_quantity}\n"
            f"SL: {plan.stop_loss}\n{tp_text}"
        )
        if price is None:
            buttons = [[
                InlineKeyboardButton(
                    "🔄 Проверить снова",
                    callback_data=f"trigger:retry:{plan.execution_id}",
                ),
                InlineKeyboardButton(
                    "❌ Отменить",
                    callback_data=f"trigger:cancel:{plan.execution_id}",
                ),
            ]]
        elif record.condition_met(price):
            text += (
                "\n\n⚠️ Текущая цена уже прошла триггер. "
                "Повторная активация заблокирована."
            )
            buttons = [[
                InlineKeyboardButton(
                    "🧮 Пересчитать вход сейчас",
                    callback_data=(
                        f"trigger:recalculate:{plan.execution_id}"
                    ),
                ),
                InlineKeyboardButton(
                    "❌ Отменить триггер",
                    callback_data=f"trigger:cancel:{plan.execution_id}",
                ),
            ]]
        else:
            buttons = [[
                InlineKeyboardButton(
                    "✅ Активировать заново",
                    callback_data=f"trigger:rearm:{plan.execution_id}",
                ),
                InlineKeyboardButton(
                    "❌ Отменить",
                    callback_data=f"trigger:cancel:{plan.execution_id}",
                ),
            ]]
        for chat_id in self.access.allowed_user_ids:
            await application.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=InlineKeyboardMarkup(buttons),
            )

    async def trigger_button_handler(self, update, context) -> None:
        if not await self._authorize(update):
            return
        query = update.callback_query
        await query.answer()
        if self.trigger_service is None:
            await query.edit_message_text("❌ Сервис триггеров выключен")
            return
        try:
            _, action, execution_id = query.data.split(":", 2)
        except ValueError:
            await query.edit_message_text("❌ Некорректная кнопка")
            return
        record = self.trigger_service.get(execution_id)
        if record is None:
            await query.edit_message_text("❌ Триггер не найден")
            return
        if action == "cancel":
            await self.trigger_service.cancel(execution_id)
            await query.edit_message_text(
                "❌ Триггер отменён; ордер на биржу не отправлялся"
                if cancelled else "ℹ️ Триггер уже не активен"
            )
            return
        if action == "retry":
            await query.edit_message_text("🔄 Проверяю цену...")
            await self._send_trigger_recovery(context.application, record)
            return
        if action == "rearm":
            ok, message, price = await self.trigger_service.rearm(execution_id)
            await query.edit_message_text(
                f"{'✅' if ok else '⚠️'} {message}\n"
                f"Текущая цена Bitunix: {price or 'недоступна'}"
            )
            return
        if action != "recalculate":
            await query.edit_message_text("❌ Неизвестное действие")
            return
        plan = record.plan
        signal = TradeSignal(
            symbol=plan.symbol,
            side=plan.side,
            entry_min=plan.entry_min,
            entry_max=plan.entry_max,
            take_profits=list(
                plan.raw_take_profits
                or tuple(item.price for item in plan.take_profits)
            ),
            stop_loss=plan.stop_loss,
        )
        try:
            account = await asyncio.to_thread(
                self.account_service.get_account, "USDT"
            )
            planner = TradePlanner(
                self.market_service,
                TradeSettings(
                    leverage=plan.leverage,
                    risk_percent=plan.risk_percent,
                    max_tp_count=len(plan.take_profits),
                    enable_emulated_triggers=False,
                ),
            )
            new_plan = await asyncio.to_thread(
                planner.create_plan, signal, account, force_market=True
            )
            proposal = TradeProposal.create(new_plan)
            ProposalService.store(context.user_data, proposal)
            cancelled = await self.trigger_service.cancel(execution_id)
            text = (
                f"🧮 Новый расчёт MARKET {new_plan.side.value} "
                f"{new_plan.symbol}\n"
                f"Текущая цена: {new_plan.current_price}\n"
                f"Объём: {new_plan.total_quantity}\n"
                f"SL: {new_plan.stop_loss}\n"
                "Старый триггер отменён. Проверьте расчёт и подтвердите "
                "в течение 5 минут."
            )
            keyboard = [[
                InlineKeyboardButton(
                    "✅ Войти MARKET",
                    callback_data=f"enter:{proposal.proposal_id}",
                ),
                InlineKeyboardButton(
                    "❌ Отмена",
                    callback_data=f"cancel:{proposal.proposal_id}",
                ),
            ]]
            await query.edit_message_text(
                text, reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception as error:
            await query.edit_message_text(f"❌ Не удалось пересчитать: {error}")

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
            "Я распознаю его и помогу войти в сделку.\n\n"
            "Список команд: /help"
        )

    async def help(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._authorize(update):
            return
        await update.effective_message.reply_text(
            "Команды бота:\n\n"
            "/mode — текущий режим торговли\n"
            "/positions — открытые позиции\n"
            "/orders — активные ордера\n"
            "/trades — последние сделки\n"
            "/stats — статистика торговли\n"
            "/export — скачать журнал сделок\n"
            "/cancel_order — отменить ордер\n"
            "/close_position — закрыть позицию\n"
            "/ping — проверить работу бота\n"
            "/restart — перезапустить процесс\n\n"
            "Для новой сделки просто отправьте текст сигнала.\n"
            "Сообщения об отмене, TP1, стопе или закрытии идеи "
            "обрабатываются как сопровождение существующей сделки."
        )

    async def ping(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._authorize(update):
            return
        await update.effective_message.reply_text("✅ Бот отвечает")

    async def restart(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._authorize(update):
            return
        await update.effective_message.reply_text(
            "🔄 Перезапускаю бот..."
        )
        await asyncio.sleep(0.5)
        os.execv(
            sys.executable,
            [sys.executable, *sys.argv],
        )
    
    async def handle_signal(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._authorize(update):
            return ConversationHandler.END
        message = update.message.text or update.message.caption
        
        if not message:
            if update.message.photo:
                return ConversationHandler.END
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
            "Выберите плечо:",
            reply_markup=self._leverage_keyboard(),
        )
        return WAITING_LEVERAGE

    async def handle_signal_update(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._authorize(update):
            raise ApplicationHandlerStop
        message = update.effective_message
        text = message.text or message.caption or ""
        signal_update = SignalUpdateParser.parse(text)
        if signal_update is None:
            return
        await self._process_signal_update(
            update,
            context,
            signal_update,
        )
        raise ApplicationHandlerStop

    async def _process_signal_update(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        signal_update: SignalUpdate,
    ) -> None:
        message = update.effective_message
        fingerprint = hashlib.sha256(
            " ".join(signal_update.raw_text.lower().split()).encode()
        ).hexdigest()
        recent_updates = getattr(self, "_recent_signal_updates", None)
        if recent_updates is None:
            recent_updates = {}
            self._recent_signal_updates = recent_updates
        now = time.monotonic()
        self._recent_signal_updates = {
            key: timestamp
            for key, timestamp in recent_updates.items()
            if now - timestamp < 600
        }
        if fingerprint in self._recent_signal_updates:
            await message.reply_text(
                "ℹ️ Это сопровождение уже было обработано."
            )
            return
        source_event_id = self._signal_update_source_id(
            update,
            fingerprint,
        )
        written = await asyncio.to_thread(
            self.journal.append,
            JournalEvent(
                event_type=signal_update.event_type.value,
                status="RECEIVED",
                symbol=signal_update.symbol,
                entry_price=signal_update.reported_price,
                pnl=(
                    f"{signal_update.reported_percent}%"
                    if signal_update.reported_percent
                    else ""
                ),
                source_event_id=source_event_id,
            ),
        )
        if not written:
            await message.reply_text(
                "ℹ️ Это сопровождение уже было обработано."
            )
            return
        try:
            orders, positions = await asyncio.gather(
                asyncio.to_thread(
                    self.order_service.get_pending_orders,
                    signal_update.symbol,
                ),
                asyncio.to_thread(
                    self.position_service.get_open_positions,
                    signal_update.symbol,
                ),
            )
        except Exception as error:
            await self._journal_signal_update_status(
                signal_update,
                source_event_id,
                "API_ERROR",
                error=type(error).__name__,
            )
            await message.reply_text(
                "⚠️ Не удалось проверить состояние Bitunix: "
                f"{type(error).__name__}. Сигнал не привёл "
                "к отмене или закрытию."
            )
            return
        self._recent_signal_updates[fingerprint] = now
        entry_orders = tuple(
            order
            for order in orders
            if not order.reduce_only
        )
        if not orders and not positions:
            await self._journal_signal_update_status(
                signal_update,
                source_event_id,
                "NO_ACTIVE_TRADE",
            )
            await message.reply_text(
                "ℹ️ Сопровождение сигнала пропущено\n\n"
                f"Символ: {signal_update.symbol}\n"
                "Открытых позиций и активных ордеров не найдено.\n"
                "Возможно, бот не входил в сделку или она уже завершена."
            )
            return
        if signal_update.event_type is SignalUpdateType.TP1_REPORTED:
            await self._handle_reported_tp1(
                message,
                signal_update,
                source_event_id,
                positions,
            )
            return
        close_positions = (
            positions
            if signal_update.requires_position_close
            else ()
        )
        cancel_orders = (
            entry_orders
            if signal_update.requires_entry_cancellation
            else ()
        )
        if not close_positions and not cancel_orders:
            await self._journal_signal_update_status(
                signal_update,
                source_event_id,
                "NO_APPLICABLE_ACTION",
            )
            detail = "Подходящих ожидающих входов для отмены нет."
            if (
                signal_update.event_type
                is SignalUpdateType.CANCEL_ENTRY
                and positions
            ):
                detail += " Открытая позиция не закрывается сигналом отмены."
            await message.reply_text(
                "ℹ️ Действие не требуется\n\n"
                f"Символ: {signal_update.symbol}\n{detail}"
            )
            return
        if close_positions and cancel_orders:
            action = ManagementAction.CLOSE_AND_CANCEL
        elif close_positions:
            action = ManagementAction.CLOSE_POSITIONS
        else:
            action = ManagementAction.CANCEL_ORDERS
        proposal = ManagementProposal.create_signal_action(
            action,
            signal_update.symbol,
            order_ids=tuple(order.order_id for order in cancel_orders),
            position_ids=tuple(
                position.position_id for position in close_positions
            ),
            signal_event_type=signal_update.event_type.value,
            source_event_id=source_event_id,
        )
        ManagementProposalService.store(context.user_data, proposal)
        text = self._format_signal_action_proposal(
            signal_update,
            close_positions,
            cancel_orders,
        )
        await self._send_management_confirmation(
            update,
            proposal,
            text,
        )

    async def _handle_reported_tp1(
        self,
        message,
        signal_update: SignalUpdate,
        source_event_id: str,
        positions,
    ) -> None:
        if not positions:
            await self._journal_signal_update_status(
                signal_update,
                source_event_id,
                "NO_ACTIVE_TRADE",
            )
            await message.reply_text(
                "ℹ️ Автор сообщил о достижении TP1, но активная "
                f"позиция {signal_update.symbol} не найдена.\n"
                "Торговые действия не выполнялись."
            )
            return
        await self._journal_signal_update_status(
            signal_update,
            source_event_id,
            "INFORMATIONAL",
        )
        auto_mode = (
            "включён"
            if self.monitoring.auto_break_even_on_tp1
            else "требует подтверждения"
        )
        await message.reply_text(
            "ℹ️ Автор сообщил о достижении TP1\n\n"
            f"Символ: {signal_update.symbol}\n"
            f"Открытых позиций: {len(positions)}\n"
            f"Автоперенос SL: {auto_mode}\n\n"
            "Исполнение TP1 и перенос SL определяются только "
            "по WebSocket-событию Bitunix. Повторный перенос не запускался."
        )

    async def _journal_signal_update_status(
        self,
        signal_update: SignalUpdate,
        source_event_id: str,
        status: str,
        *,
        error: str = "",
    ) -> None:
        await asyncio.to_thread(
            self.journal.append,
            JournalEvent(
                event_type=signal_update.event_type.value,
                status=status,
                symbol=signal_update.symbol,
                entry_price=signal_update.reported_price,
                pnl=(
                    f"{signal_update.reported_percent}%"
                    if signal_update.reported_percent
                    else ""
                ),
                error=error,
                source_event_id=f"{source_event_id}:{status}",
            ),
        )

    @staticmethod
    def _signal_update_source_id(
        update: Update,
        fingerprint: str,
    ) -> str:
        chat = update.effective_chat
        message = update.effective_message
        chat_id = getattr(chat, "id", "unknown")
        message_id = getattr(message, "message_id", None)
        identity = message_id if message_id is not None else fingerprint
        return f"telegram-signal-update:{chat_id}:{identity}"

    @staticmethod
    def _format_signal_action_proposal(
        signal_update: SignalUpdate,
        positions,
        orders,
    ) -> str:
        labels = {
            SignalUpdateType.CANCEL_ENTRY: "Отмена идеи",
            SignalUpdateType.CLOSE_MARKET: "Закрытие по текущей цене",
            SignalUpdateType.CLOSE_BREAK_EVEN: "Закрытие в безубытке",
            SignalUpdateType.CLOSE_OPPOSITE: (
                "Закрытие из-за противоположной сделки"
            ),
            SignalUpdateType.STOP_REPORTED: "Закрытие по стопу",
        }
        lines = [
            "⚠️ Получено сопровождение сигнала",
            "",
            f"Символ: {signal_update.symbol}",
            f"Событие: {labels[signal_update.event_type]}",
        ]
        if signal_update.reported_price:
            lines.append(
                f"Цена автора: {signal_update.reported_price}"
            )
        if signal_update.reported_percent:
            lines.append(
                f"Результат автора: {signal_update.reported_percent}%"
            )
        if positions:
            lines.extend(("", "Позиции бота:"))
            lines.extend(
                f"• {item.side} {item.quantity}; вход "
                f"{item.average_open_price}; PnL {item.unrealized_pnl}"
                for item in positions
            )
        if orders:
            lines.extend(("", f"Ожидающих входов к отмене: {len(orders)}"))
        if len(positions) > 1:
            lines.extend((
                "",
                "⚠️ Будут закрыты все перечисленные позиции по символу.",
            ))
        lines.extend((
            "",
            "Процент и цена автора справочные; фактический результат "
            "будет получен с Bitunix.",
        ))
        return "\n".join(lines)

    @staticmethod
    def _leverage_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "10x",
                    callback_data="leverage:10",
                ),
                InlineKeyboardButton(
                    "15x",
                    callback_data="leverage:15",
                ),
                InlineKeyboardButton(
                    "20x",
                    callback_data="leverage:20",
                ),
            ],
            [
                InlineKeyboardButton(
                    "✍️ Ввести вручную",
                    callback_data="leverage:manual",
                ),
            ],
        ])

    @staticmethod
    def _risk_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "1%",
                callback_data="risk:1",
            ),
            InlineKeyboardButton(
                "2%",
                callback_data="risk:2",
            ),
            InlineKeyboardButton(
                "3%",
                callback_data="risk:3",
            ),
        ]])

    @staticmethod
    def _tp_strategy_options(signal) -> tuple[int, ...]:
        count = len(signal.take_profits)
        if count >= 5:
            return (5, 3, 1)
        if count == 4:
            return (4, 3, 1)
        if count == 3:
            return (3, 1)
        return tuple(dict.fromkeys((count, 1)))

    @classmethod
    def _tp_strategy_keyboard(cls, signal) -> InlineKeyboardMarkup:
        labels = {
            1: "Только TP1 — 100%",
            3: "Первые 3 TP",
            5: "Все 5 TP",
        }
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(
                labels.get(count, f"Все {count} TP"),
                callback_data=f"tp_strategy:{count}",
            )]
            for count in cls._tp_strategy_options(signal)
        ])

    async def leverage_button_handler(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        if not await self._authorize(update):
            return ConversationHandler.END
        query = update.callback_query
        await query.answer()
        value = query.data.split(":", 1)[1]
        if value == "manual":
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(
                "Введите плечо числом от 1 до 125:"
            )
            return WAITING_LEVERAGE

        leverage = int(value)
        context.user_data[LEVERAGE_KEY] = leverage
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            f"✅ Плечо: {leverage}x\n"
            "Выберите риск:",
            reply_markup=self._risk_keyboard(),
        )
        return WAITING_RISK

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
            "Выберите риск:",
            reply_markup=self._risk_keyboard(),
        )
        return WAITING_RISK

    async def risk_button_handler(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        if not await self._authorize(update):
            return ConversationHandler.END
        query = update.callback_query
        await query.answer()
        risk = float(query.data.split(":", 1)[1])
        await query.edit_message_reply_markup(reply_markup=None)
        signal = context.user_data.get(SIGNAL_KEY)
        leverage = context.user_data.get(LEVERAGE_KEY)
        if signal is None or leverage is None:
            await query.message.reply_text(
                "❌ Сессия устарела. Отправьте сигнал заново."
            )
            return ConversationHandler.END

        context.user_data[RISK_KEY] = risk
        await query.message.reply_text(
            f"✅ Риск: {risk:g}%\n"
            "Сколько тейков использовать?",
            reply_markup=self._tp_strategy_keyboard(signal),
        )
        return WAITING_TP_STRATEGY

    async def tp_strategy_button_handler(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        if not await self._authorize(update):
            return ConversationHandler.END
        query = update.callback_query
        await query.answer()
        signal = context.user_data.get(SIGNAL_KEY)
        leverage = context.user_data.get(LEVERAGE_KEY)
        risk = context.user_data.get(RISK_KEY)
        if signal is None or leverage is None or risk is None:
            await query.edit_message_text(
                "❌ Сессия устарела. Отправьте сигнал заново."
            )
            return ConversationHandler.END

        tp_count = int(query.data.split(":", 1)[1])
        if tp_count not in self._tp_strategy_options(signal):
            await query.edit_message_text(
                "❌ Эта стратегия недоступна для текущего сигнала. "
                "Отправьте сигнал заново."
            )
            return ConversationHandler.END

        selected_signal = replace(
            signal,
            take_profits=list(signal.take_profits[:tp_count]),
        )
        context.user_data[SIGNAL_KEY] = selected_signal
        await query.edit_message_reply_markup(reply_markup=None)
        progress_message = await query.message.reply_text(
            f"✅ Выбрано TP: {tp_count}\n⏳ Считаю..."
        )
        return await self._calculate_selected_risk(
            query.message,
            progress_message,
            context,
            risk,
        )

    async def _calculate_selected_risk(
        self,
        message,
        progress_message,
        context,
        risk,
    ):
        signal = context.user_data.get(SIGNAL_KEY)
        leverage = context.user_data.get(LEVERAGE_KEY)
        if signal is None or leverage is None:
            await progress_message.edit_text(
                "❌ Сессия устарела. Отправьте сигнал заново."
            )
            return ConversationHandler.END

        context.user_data[RISK_KEY] = risk
        await self._calculate_and_send_proposal(
            message,
            progress_message,
            context.user_data,
            signal,
            leverage,
            risk,
        )
        return ConversationHandler.END

    async def _calculate_and_send_proposal(
        self,
        message,
        progress_message,
        user_data,
        signal,
        leverage,
        risk,
    ) -> None:
        try:
            settings = TradeSettings(
                leverage=leverage,
                risk_percent=risk,
                enable_emulated_triggers=getattr(
                    getattr(self, "triggers", None), "enabled", True
                ),
            )
            trade_service = TradeService(self.client, settings)
            account, existing_orders, existing_positions = (
                await asyncio.gather(
                    asyncio.to_thread(
                        self.account_service.get_account,
                        "USDT",
                    ),
                    asyncio.to_thread(
                        self.order_service.get_pending_orders,
                        signal.symbol,
                    ),
                    asyncio.to_thread(
                        self.position_service.get_open_positions,
                        signal.symbol,
                    ),
                )
            )
            plan = await asyncio.to_thread(
                trade_service.build_plan,
                signal,
                account,
            )
            manual_only = not plan.api_execution_supported
            proposal = None
            if manual_only:
                ProposalService.discard(user_data)
            else:
                proposal = TradeProposal.create(plan)
                ProposalService.store(user_data, proposal)
            order_info = plan.to_order_info()
            
            current = order_info["current_price"]
            planned_entry = order_info["planned_entry_price"]
            total_qty = order_info["total_quantity"]
            sl = order_info["stop_loss"]
            take_profits = order_info["take_profits"]
            tp_quantities = order_info["tp_quantities"]
            risk_budget = order_info["risk_budget"]
            position_value = total_qty * planned_entry
            
            if signal.side.value == "LONG":
                sl_loss = (planned_entry - sl) * total_qty
            else:
                sl_loss = (sl - planned_entry) * total_qty
            
            text = f"📊 *РАСЧЁТ*\n\n"
            if manual_only:
                text += (
                    "⚠️ *РУЧНОЕ РАЗМЕЩЕНИЕ*\n"
                    "Bitunix не поддерживает API-торговлю этим "
                    "инструментом. Бот ничего не отправит на биржу.\n\n"
                )
            text += f"Режим: `{self.execution.mode.value}`\n"
            text += f"*{signal.side.value} {order_info['symbol']}*\n"
            text += f"Плечо: {leverage}x | Риск: {risk}%\n"
            text += f"Текущая цена Bitunix: {current}\n"
            text += (
                "Диапазон сигнала: "
                f"{self._format_entry_range(signal)}\n"
            )
            text += f"Тип ордера: {order_info['order_type']}\n"
            if plan.is_emulated_trigger:
                text += (
                    "⚠️ Это локальный триггер: до достижения цены "
                    "ордер на Bitunix не существует. Он работает только "
                    "пока бот запущен. После триггера будет отправлен "
                    "MARKET-вход сразу с TP и SL.\n"
                )
            text += f"Плановая цена входа: {planned_entry}\n"
            text += f"Объём: {total_qty}\n"
            text += f"Позиция: {position_value:.2f} USDT\n"
            text += f"SL: {sl} (−{sl_loss:.2f} USDT)\n\n"
            text += (
                f"Риск-бюджет: {risk_budget:.2f} USDT\n"
                f"Расчётный риск: {sl_loss:.2f} USDT\n\n"
            )
            exposure_warning = self._existing_exposure_warning(
                signal.symbol,
                existing_orders,
                existing_positions,
            )
            if exposure_warning:
                text += f"{exposure_warning}\n\n"
            text += f"*Тейки:*\n"
            
            for i, (tp, qty) in enumerate(zip(take_profits, tp_quantities)):
                share = qty / total_qty * 100
                if signal.side.value == "LONG":
                    profit = (tp - planned_entry) * qty
                else:
                    profit = (planned_entry - tp) * qty
                text += f"TP{i+1}: {tp} | {qty} ({share:.0f}%) | +{profit:.2f} USDT\n"
            
            await progress_message.delete()
            await message.reply_text(text, parse_mode='Markdown')
            if manual_only:
                await message.reply_text(
                    "ℹ️ Расчёт готов для ручного ввода на Bitunix. "
                    "Кнопка автоматического входа отключена."
                )
                return
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
            await message.reply_text(
                "Подтвердите в течение 5 минут:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        except TradePlanningError as error:
            if (
                len(signal.take_profits) > 3
                and "использовать только первые 3" in str(error)
            ):
                keyboard = [[
                    InlineKeyboardButton(
                        "✅ Рассчитать с 3 TP",
                        callback_data="retry_three_tp",
                    ),
                    InlineKeyboardButton(
                        "❌ Отмена",
                        callback_data="retry_three_cancel",
                    ),
                ]]
                await progress_message.edit_text(
                    f"❌ {error}\n\n"
                    "Пересчитать сделку по первым трём тейкам?",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            else:
                await progress_message.edit_text(f"❌ {error}")
        except Exception as e:
            await progress_message.edit_text(f"❌ Ошибка: {e}")

    @staticmethod
    def _format_entry_range(signal) -> str:
        return (
            f"{min(signal.entry_min, signal.entry_max)}"
            f"–{max(signal.entry_min, signal.entry_max)}"
        )

    @staticmethod
    def _existing_exposure_warning(
        symbol,
        orders,
        positions,
    ) -> str:
        order_count = len(orders)
        position_count = len(positions)
        if not order_count and not position_count:
            return ""
        details = []
        if order_count:
            details.append(f"активных ордеров: {order_count}")
        if position_count:
            details.append(f"открытых позиций: {position_count}")
        return (
            f"⚠️ *ВНИМАНИЕ: по {symbol} уже есть "
            f"{'; '.join(details)}.*\n"
            "Новый вход может объединиться с существующей позицией "
            "и создать конфликт объёмов TP. Проверьте Bitunix "
            "перед подтверждением."
        )

    async def retry_three_tp_handler(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._authorize(update):
            return
        query = update.callback_query
        await query.answer()

        if query.data == "retry_three_cancel":
            await query.edit_message_text("❌ Расчёт отменён")
            return

        signal = context.user_data.get(SIGNAL_KEY)
        leverage = context.user_data.get(LEVERAGE_KEY)
        risk = context.user_data.get(RISK_KEY)
        if signal is None or leverage is None or risk is None:
            await query.edit_message_text(
                "❌ Сессия устарела. Отправьте сигнал заново."
            )
            return

        three_tp_signal = replace(
            signal,
            take_profits=list(signal.take_profits[:3]),
        )
        context.user_data[SIGNAL_KEY] = three_tp_signal
        await query.edit_message_text("⏳ Пересчитываю с 3 TP...")
        await self._calculate_and_send_proposal(
            query.message,
            query.message,
            context.user_data,
            three_tp_signal,
            leverage,
            risk,
        )
    
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
            if proposal.plan.is_emulated_trigger:
                if self.trigger_service is None:
                    await query.edit_message_text(
                        "❌ Сервис локальных триггеров выключен; "
                        "ордер не отправлен"
                    )
                    return
                record = await self.trigger_service.arm(proposal.plan)
                await asyncio.to_thread(
                    self.journal.append,
                    JournalEvent(
                        event_type="entry_trigger",
                        status="ARMED",
                        symbol=proposal.plan.symbol,
                        side=proposal.plan.side.value,
                        order_type="TRIGGER_MARKET",
                        entry_price=str(proposal.plan.trigger_price),
                        quantity=str(proposal.plan.total_quantity),
                        stop_loss=str(proposal.plan.stop_loss),
                        execution_id=proposal.plan.execution_id,
                        source_event_id=(
                            f"trigger:{proposal.plan.execution_id}:armed"
                        ),
                    ),
                )
                condition = (
                    "≥" if proposal.plan.side.value == "LONG" else "≤"
                )
                await query.edit_message_text(
                    "✅ Локальный триггер активирован\n\n"
                    f"{proposal.plan.side.value} {proposal.plan.symbol}\n"
                    f"Условие: цена Bitunix {condition} "
                    f"{proposal.plan.trigger_price}\n"
                    f"Последняя цена: {record.last_price}\n"
                    "До срабатывания на бирже нет входного ордера.\n"
                    "После срабатывания бот отправит MARKET-вход сразу "
                    "с TP и SL. Это работает только пока бот запущен."
                )
                return
            execution_result = await asyncio.to_thread(
                self.execution_service.execute,
                proposal.plan,
            )
            result = execution_result.to_dict()
            if self.monitor is not None and execution_result.orders:
                client_ids = self.execution_service.client_ids(
                    proposal.plan
                )
                accepted_orders = [
                    (
                        client_ids[order.tp_number - 1],
                        order.tp_number,
                    )
                    for order in execution_result.orders
                ]
                for client_id, tp_number in accepted_orders:
                    self.monitor.register_plan(
                        client_id,
                        proposal.plan,
                        tp_number=tp_number,
                    )
            await asyncio.to_thread(
                self.journal.append,
                JournalEvent(
                    event_type="execution",
                    status=execution_result.status,
                    symbol=proposal.plan.symbol,
                    side=proposal.plan.side.value,
                    order_type=proposal.plan.order_type,
                    entry_price=str(
                        proposal.plan.planned_entry_price
                    ),
                    quantity=str(proposal.plan.total_quantity),
                    leverage=str(proposal.plan.leverage),
                    risk_percent=str(
                        proposal.plan.risk_percent
                    ),
                    stop_loss=str(proposal.plan.stop_loss),
                    take_profit=str(
                        proposal.plan.take_profits[0].price
                    ),
                    proposal_id=proposal.proposal_id,
                    execution_id=proposal.plan.execution_id,
                    user_id=str(
                        update.effective_user.id
                        if update.effective_user
                        else ""
                    ),
                    mode=self.execution.mode.value,
                    order_id=(
                        execution_result.orders[0].order_id
                        if execution_result.orders
                        else ""
                    ),
                    simulated=str(
                        execution_result.simulated
                    ).lower(),
                    error=execution_result.error or "",
                ),
            )
            
            if result["success"]:
                msg = self._format_success_message(
                    proposal.plan,
                    result,
                )
                await query.edit_message_text(msg, parse_mode='Markdown')
            else:
                if result.get("partial"):
                    partial_text = (
                        "⚠️ *Позиция открыта частично*\n"
                        "Принятые части уже имеют свои TP и SL.\n"
                        "Отклонённые части повторно не "
                        "отправлялись.\n\n"
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
        if self.execution.mode is ExecutionMode.LIVE:
            text = (
                "🚨 Режим исполнения: LIVE\n"
                "Подтверждение сделки отправит реальный ордер."
            )
        else:
            text = f"🛡️ Режим исполнения: {self.execution.mode.value}"
        break_even_mode = (
            "автоматически"
            if self.monitoring.auto_break_even_on_tp1
            else "с подтверждением"
        )
        text += f"\nSL после TP1: {break_even_mode}"
        await update.message.reply_text(text)

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

    async def trades(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        if not await self._authorize(update):
            return
        summaries = await asyncio.to_thread(
            self.journal.load_trade_summaries,
            10,
        )
        if not summaries:
            await update.message.reply_text(
                "Завершённых сделок в журнале пока нет"
            )
            return
        lines = ["Последние завершённые сделки:"]
        for row in summaries:
            timestamp = str(row.get("timestamp", "")).replace(
                "T",
                " ",
            )[:19]
            lines.append(
                f"{timestamp} | {row.get('symbol', '')} "
                f"{row.get('side', '')} | "
                f"net={row.get('net_pnl', '')} | "
                f"PnL={row.get('pnl', '')}"
            )
        await update.message.reply_text("\n".join(lines))

    async def stats(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        if not await self._authorize(update):
            return
        stats = await asyncio.to_thread(
            self.journal.trade_statistics
        )
        await update.message.reply_text(
            "Статистика завершённых сделок:\n"
            f"Всего: {stats.total}\n"
            f"Прибыльных: {stats.wins}\n"
            f"Убыточных: {stats.losses}\n"
            f"Без результата: {stats.breakeven}\n"
            f"Win rate: {stats.win_rate:.2f}%\n"
            f"Realized PnL: {stats.realized_pnl}\n"
            f"Комиссии: {stats.fees}\n"
            f"Funding: {stats.funding}\n"
            f"Чистый PnL: {stats.net_pnl}"
        )

    async def export_journal(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        if not await self._authorize(update):
            return
        snapshot = await asyncio.to_thread(
            self.journal.csv_snapshot
        )
        document = BytesIO(snapshot)
        document.name = "trade_journal.csv"
        await update.message.reply_document(
            document=document,
            filename="trade_journal.csv",
            caption="CSV-журнал сделок",
        )

    async def cancel_order(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        if not await self._authorize(update):
            return
        if not context.args:
            await self._send_order_selection(update)
            return
        if len(context.args) != 2:
            await update.message.reply_text(
                "Использование: /cancel_order\n"
                "или /cancel_order SYMBOL ORDER_ID"
            )
            return
        symbol, order_id = context.args
        orders = await asyncio.to_thread(
            self.order_service.get_pending_orders,
            symbol.upper(),
            order_id,
        )
        order = next(
            (
                item for item in orders
                if item.order_id == order_id
                and item.symbol.upper() == symbol.upper()
            ),
            None,
        )
        if order is None:
            await update.message.reply_text(
                "⚠️ Активный ордер с таким ID и символом не найден."
            )
            return
        await self._propose_order_cancellation(
            update,
            context,
            order,
        )

    async def _send_order_selection(self, update: Update) -> None:
        try:
            orders = await asyncio.to_thread(
                self.order_service.get_pending_orders
            )
        except Exception as error:
            await update.message.reply_text(f"❌ Ошибка: {error}")
            return
        if not orders:
            await update.message.reply_text("Активных ордеров нет")
            return
        keyboard = [
            [InlineKeyboardButton(
                (
                    f"❌ {order.side} {order.symbol} | "
                    f"{order.quantity} @ {order.price}"
                ),
                callback_data=(
                    f"manage:select_order:{order.order_id}"
                ),
            )]
            for order in orders
        ]
        await update.message.reply_text(
            "Какой ордер отменить?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _propose_order_cancellation(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        order,
        *,
        edit: bool = False,
    ) -> None:
        proposal = ManagementProposal.create(
            ManagementAction.CANCEL_ORDER,
            order.order_id,
            symbol=order.symbol.upper(),
        )
        ManagementProposalService.store(context.user_data, proposal)
        text = (
            "Отменить ордер?\n"
            f"Символ: {order.symbol}\n"
            f"Сторона: {order.side}\n"
            f"Тип: {order.order_type}\n"
            f"Объём: {order.quantity}\n"
            f"Цена: {order.price}\n"
            f"ID: {order.order_id}"
        )
        await self._send_management_confirmation(
            update,
            proposal,
            text,
            edit=edit,
        )

    async def close_position(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        if not await self._authorize(update):
            return
        if not context.args:
            await self._send_position_selection(update)
            return
        if len(context.args) != 1:
            await update.message.reply_text(
                "Использование: /close_position\n"
                "или /close_position POSITION_ID"
            )
            return
        positions = await asyncio.to_thread(
            self.position_service.get_open_positions,
            None,
            context.args[0],
        )
        position = next(
            (
                item for item in positions
                if item.position_id == context.args[0]
            ),
            None,
        )
        if position is None:
            await update.message.reply_text(
                "⚠️ Открытая позиция с таким ID не найдена."
            )
            return
        await self._propose_position_close(
            update,
            context,
            position,
        )

    async def _send_position_selection(self, update: Update) -> None:
        try:
            positions = await asyncio.to_thread(
                self.position_service.get_open_positions
            )
        except Exception as error:
            await update.message.reply_text(f"❌ Ошибка: {error}")
            return
        if not positions:
            await update.message.reply_text("Открытых позиций нет")
            return
        keyboard = [
            [InlineKeyboardButton(
                (
                    f"❌ {position.side} {position.symbol} | "
                    f"{position.quantity} | "
                    f"PnL {position.unrealized_pnl}"
                ),
                callback_data=(
                    f"manage:select_position:{position.position_id}"
                ),
            )]
            for position in positions
        ]
        await update.message.reply_text(
            "Какую позицию закрыть полностью?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _propose_position_close(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        position,
        *,
        edit: bool = False,
    ) -> None:
        proposal = ManagementProposal.create(
            ManagementAction.CLOSE_POSITION,
            position.position_id,
            symbol=position.symbol.upper(),
        )
        ManagementProposalService.store(context.user_data, proposal)
        text = (
            "Полностью закрыть позицию?\n"
            f"Символ: {position.symbol}\n"
            f"Сторона: {position.side}\n"
            f"Объём: {position.quantity}\n"
            f"Средняя цена входа: {position.average_open_price}\n"
            f"Нереализованный PnL: {position.unrealized_pnl}\n"
            f"ID: {position.position_id}"
        )
        await self._send_management_confirmation(
            update,
            proposal,
            text,
            edit=edit,
        )

    async def _send_management_confirmation(
        self,
        update: Update,
        proposal: ManagementProposal,
        text: str,
        *,
        edit: bool = False,
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
        kwargs = {
            "text": f"{text}\nПодтвердите в течение 5 минут.",
            "reply_markup": InlineKeyboardMarkup(keyboard),
        }
        if edit:
            await update.callback_query.edit_message_text(**kwargs)
        else:
            await update.message.reply_text(**kwargs)

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
            prefix, decision, target_id = query.data.split(":", 2)
            if prefix != "manage" or decision not in {
                "select_order",
                "select_position",
                "confirm",
                "cancel",
            }:
                raise ValueError
        except ValueError:
            await query.edit_message_text("❌ Некорректная кнопка")
            return
        if decision == "select_order":
            try:
                orders = await asyncio.to_thread(
                    self.order_service.get_pending_orders,
                    None,
                    target_id,
                )
                order = next(
                    (
                        item for item in orders
                        if item.order_id == target_id
                    ),
                    None,
                )
                if order is None:
                    await query.edit_message_text(
                        "⚠️ Ордер уже исполнен, отменён или не найден."
                    )
                    return
                await self._propose_order_cancellation(
                    update,
                    context,
                    order,
                    edit=True,
                )
            except Exception as error:
                await query.edit_message_text(f"❌ Ошибка: {error}")
            return
        if decision == "select_position":
            try:
                positions = await asyncio.to_thread(
                    self.position_service.get_open_positions,
                    None,
                    target_id,
                )
                position = next(
                    (
                        item for item in positions
                        if item.position_id == target_id
                    ),
                    None,
                )
                if position is None:
                    await query.edit_message_text(
                        "⚠️ Позиция уже закрыта или не найдена."
                    )
                    return
                await self._propose_position_close(
                    update,
                    context,
                    position,
                    edit=True,
                )
            except Exception as error:
                await query.edit_message_text(f"❌ Ошибка: {error}")
            return
        try:
            proposal = ManagementProposalService.consume(
                context.user_data,
                target_id,
            )
        except ManagementProposalError as error:
            await query.edit_message_text(f"❌ {error}")
            return
        if decision == "cancel":
            if proposal.signal_event_type:
                await asyncio.to_thread(
                    self.journal.append,
                    JournalEvent(
                        event_type=proposal.signal_event_type,
                        status="ACTION_DECLINED",
                        symbol=proposal.symbol or "",
                        proposal_id=proposal.proposal_id,
                        source_event_id=(
                            f"{proposal.source_event_id}:declined:"
                            f"{proposal.proposal_id}"
                        ),
                    ),
                )
            await query.edit_message_text("❌ Операция отменена")
            return

        await query.edit_message_text("⏳ Выполняю...")
        try:
            if proposal.action in {
                ManagementAction.CANCEL_ORDERS,
                ManagementAction.CLOSE_POSITIONS,
                ManagementAction.CLOSE_AND_CANCEL,
            }:
                await self._execute_signal_management(query, proposal)
            elif proposal.action is ManagementAction.CANCEL_ORDER:
                orders = await asyncio.to_thread(
                    self.order_service.get_pending_orders,
                    proposal.symbol,
                    proposal.target_id,
                )
                if not any(
                    order.order_id == proposal.target_id
                    and order.symbol.upper() == proposal.symbol
                    for order in orders
                ):
                    await query.edit_message_text(
                        "⚠️ Ордер уже исполнен, отменён или изменился. "
                        "Отмена не отправлена."
                    )
                    return
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
            elif proposal.action is ManagementAction.CLOSE_POSITION:
                positions = await asyncio.to_thread(
                    self.position_service.get_open_positions,
                    proposal.symbol,
                    proposal.target_id,
                )
                if not any(
                    position.position_id == proposal.target_id
                    and position.symbol.upper() == proposal.symbol
                    for position in positions
                ):
                    await query.edit_message_text(
                        "⚠️ Позиция уже закрыта или изменилась. "
                        "Закрытие не отправлено."
                    )
                    return
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
            else:
                await query.edit_message_text(
                    "❌ Неизвестный тип операции"
                )
        except Exception as error:
            if proposal.signal_event_type:
                await asyncio.to_thread(
                    self.journal.append,
                    JournalEvent(
                        event_type=proposal.signal_event_type,
                        status="ACTION_ERROR",
                        symbol=proposal.symbol or "",
                        error=type(error).__name__,
                        proposal_id=proposal.proposal_id,
                        source_event_id=(
                            f"{proposal.source_event_id}:action-error:"
                            f"{proposal.proposal_id}"
                        ),
                    ),
                )
            await query.edit_message_text(f"❌ Ошибка: {error}")

    async def _execute_signal_management(
        self,
        query,
        proposal: ManagementProposal,
    ) -> None:
        active_order_ids: tuple[str, ...] = ()
        active_position_ids: tuple[str, ...] = ()
        if proposal.order_ids:
            orders = await asyncio.to_thread(
                self.order_service.get_pending_orders,
                proposal.symbol,
            )
            requested_order_ids = set(proposal.order_ids)
            active_order_ids = tuple(
                order.order_id
                for order in orders
                if (
                    order.order_id in requested_order_ids
                    and order.symbol.upper() == proposal.symbol
                    and not order.reduce_only
                )
            )
        if proposal.position_ids:
            positions = await asyncio.to_thread(
                self.position_service.get_open_positions,
                proposal.symbol,
            )
            requested_position_ids = set(proposal.position_ids)
            active_position_ids = tuple(
                position.position_id
                for position in positions
                if (
                    position.position_id in requested_position_ids
                    and position.symbol.upper() == proposal.symbol
                )
            )
        if not active_order_ids and not active_position_ids:
            await asyncio.to_thread(
                self.journal.append,
                JournalEvent(
                    event_type=proposal.signal_event_type,
                    status="NO_LONGER_ACTIVE",
                    symbol=proposal.symbol or "",
                    proposal_id=proposal.proposal_id,
                    source_event_id=(
                        f"{proposal.source_event_id}:no-longer-active:"
                        f"{proposal.proposal_id}"
                    ),
                ),
            )
            await query.edit_message_text(
                "ℹ️ Позиции и ордера уже не активны. "
                "Новые команды на Bitunix не отправлялись."
            )
            return
        messages = []
        simulated_results = []
        if active_order_ids:
            cancel_result = await asyncio.to_thread(
                self.order_service.cancel_orders,
                proposal.symbol,
                active_order_ids,
            )
            simulated_results.append(cancel_result.simulated)
            messages.append(
                f"Ожидающих входов отменено/отправлено: "
                f"{len(active_order_ids)}"
            )
            if cancel_result.failed:
                messages.append(
                    f"⚠️ Bitunix отклонил отмен: "
                    f"{len(cancel_result.failed)}"
                )
                await asyncio.to_thread(
                    self.journal.append,
                    JournalEvent(
                        event_type=proposal.signal_event_type,
                        status="ACTION_PARTIAL",
                        symbol=proposal.symbol or "",
                        proposal_id=proposal.proposal_id,
                        order_id=",".join(active_order_ids),
                        error=(
                            f"cancel failures: "
                            f"{len(cancel_result.failed)}"
                        ),
                        simulated=str(cancel_result.simulated),
                        source_event_id=(
                            f"{proposal.source_event_id}:partial:"
                            f"{proposal.proposal_id}"
                        ),
                    ),
                )
                await query.edit_message_text(
                    "⚠️ Не все ожидающие входы удалось отменить.\n"
                    "Закрытие позиции не отправлялось, чтобы "
                    "оставшийся ордер не открыл её повторно.\n"
                    "Проверьте ордера и позицию на Bitunix."
                )
                return
        for position_id in active_position_ids:
            close_result = await asyncio.to_thread(
                self.position_service.close_position,
                position_id,
            )
            simulated_results.append(close_result.simulated)
        if active_position_ids:
            messages.append(
                f"Позиций закрыто/отправлено на закрытие: "
                f"{len(active_position_ids)}"
            )
        simulated = bool(simulated_results) and all(simulated_results)
        await asyncio.to_thread(
            self.journal.append,
            JournalEvent(
                event_type=proposal.signal_event_type,
                status="ACTION_COMPLETED",
                symbol=proposal.symbol or "",
                proposal_id=proposal.proposal_id,
                order_id=",".join(active_order_ids),
                position_id=",".join(active_position_ids),
                simulated=str(simulated),
                source_event_id=(
                    f"{proposal.source_event_id}:action:"
                    f"{proposal.proposal_id}"
                ),
            ),
        )
        mode_text = (
            "Симуляция завершена."
            if simulated
            else "Команды отправлены; финальный статус ожидается."
        )
        await query.edit_message_text(
            "✅ Сопровождение выполнено\n\n"
            + "\n".join(messages)
            + f"\n{mode_text}"
        )

    async def break_even_button_handler(
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
            if (
                prefix != "breakeven"
                or decision not in {"confirm", "cancel"}
            ):
                raise ValueError
        except ValueError:
            await query.edit_message_text("❌ Некорректная кнопка")
            return
        if self.monitor is None:
            await query.edit_message_text(
                "❌ Мониторинг позиции недоступен"
            )
            return
        if decision == "confirm":
            await query.edit_message_text(
                "⏳ Проверяю позицию и пересчитываю безубыток..."
            )
        try:
            result = await self.monitor.confirm_break_even(
                proposal_id,
                decision == "confirm",
            )
            icon = "✅" if decision == "confirm" else "ℹ️"
            await query.edit_message_text(f"{icon} {result}")
        except ValueError as error:
            await query.edit_message_text(f"❌ {error}")
        except Exception as error:
            await query.edit_message_text(
                "❌ Не удалось изменить SL: "
                f"{type(error).__name__}. Проверьте позицию на Bitunix."
            )

    @staticmethod
    def _reset_user_state(user_data):
        user_data.pop(SIGNAL_KEY, None)
        user_data.pop(LEVERAGE_KEY, None)
        user_data.pop(RISK_KEY, None)
        ProposalService.discard(user_data)

    @staticmethod
    def _format_success_message(plan, result: dict) -> str:
        if result["simulated"]:
            header = (
                "🧪 *Симуляция завершена*\n"
                "Ордер не отправлялся на Bitunix."
            )
            status = "симуляция"
        elif plan.order_type == "LIMIT":
            header = "✅ *Пакет лимитных ордеров отправлен!*"
            status = "ожидает исполнения"
        else:
            header = "✅ *Пакет рыночных ордеров отправлен!*"
            status = "исполнение подтверждается Bitunix"

        tp_lines = "\n".join(
            f"TP{index}: {take_profit.price} — "
            f"{take_profit.quantity} "
            f"({take_profit.quantity / plan.total_quantity * 100:.0f}%)"
            for index, take_profit in enumerate(
                plan.take_profits,
                start=1,
            )
        )
        return (
            f"{header}\n\n"
            f"Тип ордера: {plan.order_type}\n"
            f"Вход: {plan.planned_entry_price}\n"
            f"Объём: {plan.total_quantity}\n"
            f"{tp_lines}\n"
            f"SL: {result['stop_loss']}\n"
            f"Статус входа: {status}\n"
            "✅ Каждый вход уже отправлен со своим TP и SL.\n"
            "Защита хранится на Bitunix и не зависит от работы "
            "бота.\n"
            "👀 Мониторинг используется для уведомлений, журнала "
            "и проверки защиты."
        )


def main():
    bot = FuturesBot()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    proxy_url = os.getenv("TELEGRAM_PROXY", None)
    
    builder = (
        Application.builder()
        .token(token)
        .post_init(bot.post_init)
        .post_shutdown(bot.post_shutdown)
    )
    if proxy_url:
        builder = builder.proxy_url(proxy_url)
    else:
        builder = builder.connect_timeout(30).read_timeout(30).write_timeout(30)
    
    app = builder.build()
    app.add_error_handler(telegram_error_handler)

    signal_update_filter = (
        filters.Regex(SIGNAL_UPDATE_PATTERN)
        | filters.CaptionRegex(SIGNAL_UPDATE_PATTERN)
    )
    app.add_handler(
        MessageHandler(
            signal_update_filter & ~filters.COMMAND,
            bot.handle_signal_update,
        ),
        group=-1,
    )
    
    conv_handler = ConversationHandler(
        entry_points=[MessageHandler((filters.TEXT | filters.PHOTO | filters.CAPTION) & ~filters.COMMAND, bot.handle_signal)],
        states={
            WAITING_LEVERAGE: [
                CallbackQueryHandler(
                    bot.leverage_button_handler,
                    pattern=r"^leverage:(10|15|20|manual)$",
                ),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    bot.set_leverage,
                ),
            ],
            WAITING_RISK: [
                CallbackQueryHandler(
                    bot.risk_button_handler,
                    pattern=r"^risk:(1|2|3)$",
                ),
            ],
            WAITING_TP_STRATEGY: [
                CallbackQueryHandler(
                    bot.tp_strategy_button_handler,
                    pattern=r"^tp_strategy:(1|2|3|4|5)$",
                ),
            ],
        },
        fallbacks=[CommandHandler("start", bot.start)],
    )
    
    app.add_handler(CommandHandler("start", bot.start))
    app.add_handler(CommandHandler("help", bot.help))
    app.add_handler(CommandHandler("mode", bot.mode))
    app.add_handler(CommandHandler("positions", bot.positions))
    app.add_handler(CommandHandler("orders", bot.orders))
    app.add_handler(CommandHandler("trades", bot.trades))
    app.add_handler(CommandHandler("stats", bot.stats))
    app.add_handler(CommandHandler("export", bot.export_journal))
    app.add_handler(CommandHandler("cancel_order", bot.cancel_order))
    app.add_handler(CommandHandler("close_position", bot.close_position))
    app.add_handler(CommandHandler("ping", bot.ping))
    app.add_handler(CommandHandler("restart", bot.restart))
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(
        bot.management_button_handler,
        pattern=r"^manage:",
    ))
    app.add_handler(CallbackQueryHandler(
        bot.break_even_button_handler,
        pattern=r"^breakeven:",
    ))
    app.add_handler(CallbackQueryHandler(
        bot.trigger_button_handler,
        pattern=r"^trigger:",
    ))
    app.add_handler(CallbackQueryHandler(
        bot.retry_three_tp_handler,
        pattern=r"^retry_three_(tp|cancel)$",
    ))
    app.add_handler(CallbackQueryHandler(
        bot.button_handler,
        pattern=r"^(enter|cancel):",
    ))
    
    print("🤖 Бот запущен...")
    print(f"🛡️ Режим исполнения: {bot.execution.mode.value}")
    if bot.execution.mode is ExecutionMode.LIVE:
        print("🚨 LIVE: ПОДТВЕРЖДЁННЫЕ СДЕЛКИ БУДУТ РЕАЛЬНЫМИ")
    app.run_polling()

if __name__ == "__main__":
    main()
