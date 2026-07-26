from decimal import (
    Decimal,
    InvalidOperation,
    ROUND_DOWN,
    ROUND_UP,
)

from config.settings import TradeSettings
from core.api_models import AccountBalance, TradingPair
from models.signal import OrderSide, TradeSignal
from models.trade import (
    PlannedTakeProfit,
    TradePlan,
    TradePlanningError,
)
from services.market_service import MarketService


class TradePlanner:
    def __init__(
        self,
        market: MarketService,
        settings: TradeSettings,
    ):
        self.market = market
        self.settings = settings

    def calculate_position_size(
        self,
        signal: TradeSignal,
        account: AccountBalance,
        current_price: float | None = None,
        instrument: TradingPair | None = None,
    ) -> float:
        if current_price is None:
            ticker = self.market.get_ticker(signal.symbol)
            if not ticker:
                raise TradePlanningError(
                    "Не удалось получить цену"
                )
            current_price = ticker.last_price
        if instrument is None:
            instrument = self.market.get_trading_pair(signal.symbol)

        quantity, _ = self._position_metrics(
            signal,
            account,
            current_price,
            instrument,
            Decimal(str(signal.stop_loss)),
        )
        return float(quantity)

    def _position_metrics(
        self,
        signal: TradeSignal,
        account: AccountBalance,
        current_price: float,
        instrument: TradingPair,
        normalized_stop_loss: Decimal,
    ) -> tuple[Decimal, Decimal]:
        try:
            available = Decimal(account.available)
            price = Decimal(str(current_price))
            stop_loss = normalized_stop_loss
            risk_percent = Decimal(str(self.settings.risk_percent))
            leverage = Decimal(str(self.settings.leverage))
        except (InvalidOperation, TypeError) as error:
            raise TradePlanningError(
                "Некорректные данные для расчёта риска"
            ) from error

        if available <= 0:
            raise TradePlanningError("Недостаточно средств")
        if price <= 0:
            raise TradePlanningError("Цена должна быть положительной")
        if risk_percent <= 0:
            raise TradePlanningError(
                "Процент риска должен быть положительным"
            )
        if leverage <= 0:
            raise TradePlanningError(
                "Плечо должно быть положительным"
            )
        if not (
            instrument.min_leverage
            <= self.settings.leverage
            <= instrument.max_leverage
        ):
            raise TradePlanningError(
                "Плечо вне диапазона инструмента: "
                f"{instrument.min_leverage}-"
                f"{instrument.max_leverage}"
            )
        stop_distance = abs(price - stop_loss)
        if stop_distance == 0:
            raise TradePlanningError(
                "Расстояние до стоп-лосса не может быть нулевым"
            )
        if signal.side is OrderSide.LONG and stop_loss > price:
            raise TradePlanningError(
                "Для LONG стоп-лосс должен быть ниже цены входа"
            )
        if signal.side is OrderSide.SHORT and stop_loss < price:
            raise TradePlanningError(
                "Для SHORT стоп-лосс должен быть выше цены входа"
            )

        risk_budget = available * risk_percent / Decimal("100")
        quantity_by_risk = risk_budget / stop_distance
        quantity_by_margin = available * leverage / price
        maximum_market_quantity = Decimal(
            instrument.max_market_order_volume
        )
        quantity = min(
            quantity_by_risk,
            quantity_by_margin,
            maximum_market_quantity,
        )
        quantity_step = Decimal("1").scaleb(
            -instrument.base_precision
        )
        quantity = quantity.quantize(
            quantity_step,
            rounding=ROUND_DOWN,
        )

        if quantity <= 0:
            raise TradePlanningError(
                "Рассчитанный объём позиции равен нулю"
            )
        if quantity < Decimal(instrument.min_trade_volume):
            raise TradePlanningError(
                "Рассчитанный объём меньше минимального: "
                f"{instrument.min_trade_volume}"
            )

        return quantity, risk_budget

    def create_plan(
        self,
        signal: TradeSignal,
        account: AccountBalance,
    ) -> TradePlan:
        ticker = self.market.get_ticker(signal.symbol)
        if not ticker:
            raise TradePlanningError("Не удалось получить цену")

        current_price = ticker.last_price
        instrument = self.market.get_trading_pair(signal.symbol)
        if instrument.symbol_status != "OPEN":
            raise TradePlanningError(
                f"Инструмент недоступен: {instrument.symbol_status}"
            )
        if not instrument.api_supported:
            raise TradePlanningError(
                "Инструмент не поддерживает API-торговлю"
            )

        price_step = Decimal("1").scaleb(
            -instrument.quote_precision
        )
        stop_rounding = (
            ROUND_UP
            if signal.side is OrderSide.LONG
            else ROUND_DOWN
        )
        normalized_stop = Decimal(
            str(signal.stop_loss)
        ).quantize(price_step, rounding=stop_rounding)
        entry_min = min(signal.entry_min, signal.entry_max)
        entry_max = max(signal.entry_min, signal.entry_max)
        in_range = entry_min <= current_price <= entry_max

        quantity, risk_budget = self._position_metrics(
            signal,
            account,
            current_price,
            instrument,
            normalized_stop,
        )
        total_quantity = float(quantity)

        if not signal.take_profits:
            raise TradePlanningError("Не указан тейк-профит")
        normalized_take_profit = Decimal(
            str(signal.take_profits[0])
        ).quantize(
            price_step,
            rounding=(
                ROUND_DOWN
                if signal.side is OrderSide.LONG
                else ROUND_UP
            ),
        )
        if (
            signal.side is OrderSide.LONG
            and normalized_take_profit <= Decimal(str(current_price))
        ):
            raise TradePlanningError(
                "Для LONG тейк-профит должен быть выше цены"
            )
        if (
            signal.side is OrderSide.SHORT
            and normalized_take_profit >= Decimal(str(current_price))
        ):
            raise TradePlanningError(
                "Для SHORT тейк-профит должен быть ниже цены"
            )
        take_profits = (
            PlannedTakeProfit(
                price=float(normalized_take_profit),
                quantity=total_quantity,
            ),
        )

        return TradePlan(
            symbol=signal.symbol,
            side=signal.side,
            entry_min=entry_min,
            entry_max=entry_max,
            current_price=current_price,
            in_range=in_range,
            total_quantity=total_quantity,
            stop_loss=float(normalized_stop),
            take_profits=take_profits,
            leverage=self.settings.leverage,
            risk_percent=self.settings.risk_percent,
            risk_budget=float(risk_budget),
        )
