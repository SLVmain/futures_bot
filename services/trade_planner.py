from decimal import (
    Decimal,
    InvalidOperation,
    ROUND_DOWN,
    ROUND_HALF_UP,
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
        limit_price = None
        planned_entry = Decimal(str(current_price))
        if not in_range:
            planned_entry = (
                (
                    Decimal(str(entry_min))
                    + Decimal(str(entry_max))
                )
                / Decimal("2")
            ).quantize(price_step, rounding=ROUND_HALF_UP)
            limit_price = float(planned_entry)

        quantity, risk_budget = self._position_metrics(
            signal,
            account,
            float(planned_entry),
            instrument,
            normalized_stop,
        )
        total_quantity = float(quantity)

        selected_prices = signal.take_profits[
            :self.settings.max_tp_count
        ]
        if not selected_prices:
            raise TradePlanningError("Не указан тейк-профит")
        distributions = {
            1: (100,),
            2: (60, 40),
            3: (50, 30, 20),
            4: (40, 30, 20, 10),
            5: (40, 25, 15, 10, 10),
        }
        shares = distributions[len(selected_prices)]
        quantity_step = Decimal("1").scaleb(
            -instrument.base_precision
        )
        remaining = quantity
        planned = []
        for index, (raw_price, share) in enumerate(
            zip(selected_prices, shares),
            start=1,
        ):
            price = Decimal(str(raw_price)).quantize(
                price_step,
                rounding=(
                    ROUND_DOWN
                    if signal.side is OrderSide.LONG
                    else ROUND_UP
                ),
            )
            invalid = (
                signal.side is OrderSide.LONG
                and price <= planned_entry
            ) or (
                signal.side is OrderSide.SHORT
                and price >= planned_entry
            )
            if invalid:
                raise TradePlanningError(
                    f"TP{index} расположен с неверной стороны "
                    f"от цены входа. TP: {price}; "
                    f"цена входа: {planned_entry}; "
                    f"текущая цена Bitunix: {current_price}"
                )
            if index == len(shares):
                tp_quantity = remaining
            else:
                tp_quantity = (
                    quantity * Decimal(share) / Decimal("100")
                ).quantize(quantity_step, rounding=ROUND_DOWN)
                remaining -= tp_quantity
            if tp_quantity < Decimal(instrument.min_trade_volume):
                suggestion = ""
                if len(selected_prices) > 3:
                    suggestion = (
                        ". Попробуйте использовать только первые "
                        "3 тейк-профита"
                    )
                raise TradePlanningError(
                    f"Объём TP{index} меньше минимального"
                    f"{suggestion}"
                )
            planned.append(PlannedTakeProfit(
                price=float(price),
                quantity=float(tp_quantity),
            ))
        take_profits = tuple(planned)

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
            limit_price=limit_price,
        )
