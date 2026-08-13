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

        price_step = Decimal("1").scaleb(
            -instrument.quote_precision
        )
        signal_stop = self._normalize_stop_loss(
            signal.side,
            signal.stop_loss,
            price_step,
        )
        effective_stop = self._effective_stop_loss(
            signal.side,
            Decimal(str(current_price)),
            signal_stop,
            price_step,
        )

        quantity, _ = self._position_metrics(
            signal,
            account,
            current_price,
            instrument,
            effective_stop,
            signal_stop,
        )
        return float(quantity)

    def _position_metrics(
        self,
        signal: TradeSignal,
        account: AccountBalance,
        current_price: float,
        instrument: TradingPair,
        normalized_stop_loss: Decimal,
        signal_stop_loss: Decimal | None = None,
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

        per_unit_loss = stop_distance
        if self.settings.max_stop_roi_percent is not None:
            fee_rate = self._taker_fee_rate()
            per_unit_loss += (price + stop_loss) * fee_rate

        risk_budget = available * risk_percent / Decimal("100")
        quantity_by_risk = risk_budget / per_unit_loss
        if (
            self.settings.max_stop_roi_percent is not None
            and signal_stop_loss is not None
        ):
            signal_stop_distance = abs(price - signal_stop_loss)
            quantity_by_risk = min(
                quantity_by_risk,
                risk_budget / signal_stop_distance,
            )
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

    @staticmethod
    def _normalize_stop_loss(
        side: OrderSide,
        stop_loss: float,
        price_step: Decimal,
    ) -> Decimal:
        return Decimal(str(stop_loss)).quantize(
            price_step,
            rounding=(
                ROUND_UP
                if side is OrderSide.LONG
                else ROUND_DOWN
            ),
        )

    def _taker_fee_rate(self) -> Decimal:
        try:
            fee_rate = Decimal(str(self.settings.taker_fee_rate))
        except (InvalidOperation, TypeError) as error:
            raise TradePlanningError(
                "Некорректная комиссия taker"
            ) from error
        if not Decimal("0") <= fee_rate < Decimal("1"):
            raise TradePlanningError(
                "Комиссия taker должна быть от 0 до 1"
            )
        return fee_rate

    def _effective_stop_loss(
        self,
        side: OrderSide,
        entry: Decimal,
        signal_stop: Decimal,
        price_step: Decimal,
    ) -> Decimal:
        if signal_stop == entry:
            raise TradePlanningError(
                "Расстояние до стоп-лосса не может быть нулевым"
            )
        if side is OrderSide.LONG and signal_stop >= entry:
            raise TradePlanningError(
                "Для LONG стоп-лосс должен быть ниже цены входа"
            )
        if side is OrderSide.SHORT and signal_stop <= entry:
            raise TradePlanningError(
                "Для SHORT стоп-лосс должен быть выше цены входа"
            )

        raw_limit = self.settings.max_stop_roi_percent
        if raw_limit is None:
            return signal_stop
        try:
            roi_limit = Decimal(str(raw_limit)) / Decimal("100")
            leverage = Decimal(str(self.settings.leverage))
        except (InvalidOperation, TypeError) as error:
            raise TradePlanningError(
                "Некорректный лимит ROI для стоп-лосса"
            ) from error
        if not Decimal("0") < roi_limit <= Decimal("1"):
            raise TradePlanningError(
                "Лимит ROI для стоп-лосса должен быть от 0 до 100%"
            )
        if leverage <= 0:
            raise TradePlanningError(
                "Плечо должно быть положительным"
            )

        fee_rate = self._taker_fee_rate()
        per_notional_budget = roi_limit / leverage
        if per_notional_budget <= fee_rate * Decimal("2"):
            raise TradePlanningError(
                f"Лимит −{raw_limit:g}% ROI слишком мал для "
                f"плеча {self.settings.leverage}x: расчётные комиссии "
                "открытия и закрытия уже исчерпывают лимит"
            )

        if side is OrderSide.LONG:
            raw_roi_stop = entry * (
                Decimal("1") + fee_rate - per_notional_budget
            ) / (Decimal("1") - fee_rate)
            roi_stop = raw_roi_stop.quantize(
                price_step,
                rounding=ROUND_UP,
            )
            if roi_stop >= entry:
                raise TradePlanningError(
                    "Шаг цены инструмента слишком велик для "
                    "стоп-лосса с выбранным лимитом ROI"
                )
            return max(signal_stop, roi_stop)

        raw_roi_stop = entry * (
            Decimal("1") - fee_rate + per_notional_budget
        ) / (Decimal("1") + fee_rate)
        roi_stop = raw_roi_stop.quantize(
            price_step,
            rounding=ROUND_DOWN,
        )
        if roi_stop <= entry:
            raise TradePlanningError(
                "Шаг цены инструмента слишком велик для "
                "стоп-лосса с выбранным лимитом ROI"
            )
        return min(signal_stop, roi_stop)

    def create_plan(
        self,
        signal: TradeSignal,
        account: AccountBalance,
        *,
        force_market: bool = False,
        limit_price_override: float | None = None,
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
        price_step = Decimal("1").scaleb(
            -instrument.quote_precision
        )
        signal_stop = self._normalize_stop_loss(
            signal.side,
            signal.stop_loss,
            price_step,
        )
        entry_min = min(signal.entry_min, signal.entry_max)
        entry_max = max(signal.entry_min, signal.entry_max)
        in_range = entry_min <= current_price <= entry_max
        limit_price = None
        trigger_price = None
        market_price = Decimal(str(current_price))
        planned_entry = market_price
        if limit_price_override is not None:
            if force_market:
                raise TradePlanningError(
                    "Нельзя одновременно задать MARKET и LIMIT-вход"
                )
            override_rounding = (
                ROUND_UP
                if signal.side is OrderSide.LONG
                else ROUND_DOWN
            )
            planned_entry = Decimal(
                str(limit_price_override)
            ).quantize(price_step, rounding=override_rounding)
            if planned_entry <= 0:
                raise TradePlanningError(
                    "Цена лимитного входа должна быть положительной"
                )
            limit_price = float(planned_entry)
            in_range = False
        elif not in_range and not force_market:
            planned_entry = (
                (
                    Decimal(str(entry_min))
                    + Decimal(str(entry_max))
                )
                / Decimal("2")
            ).quantize(price_step, rounding=ROUND_HALF_UP)
            immediately_executable = (
                signal.side is OrderSide.LONG
                and planned_entry >= market_price
            ) or (
                signal.side is OrderSide.SHORT
                and planned_entry <= market_price
            )
            if immediately_executable:
                if not self.settings.enable_emulated_triggers:
                    raise TradePlanningError(
                        "Автоматический вход заблокирован: обычный "
                        f"{signal.side.value} LIMIT по цене "
                        f"{planned_entry} при текущей цене Bitunix "
                        f"{current_price} может исполниться немедленно. "
                        "Для такого входа нужен Trigger/Stop-Limit; "
                        "ордер не отправлен"
                    )
                trigger_price = float(planned_entry)
            else:
                limit_price = float(planned_entry)
        elif force_market:
            in_range = True

        normalized_stop = self._effective_stop_loss(
            signal.side,
            planned_entry,
            signal_stop,
            price_step,
        )

        quantity, risk_budget = self._position_metrics(
            signal,
            account,
            float(planned_entry),
            instrument,
            normalized_stop,
            signal_stop,
        )
        total_quantity = float(quantity)

        selected_prices = signal.take_profits[
            :self.settings.max_tp_count
        ]
        if not selected_prices:
            raise TradePlanningError("Не указан тейк-профит")
        if self.settings.tp_offset_ticks < 0:
            raise TradePlanningError(
                "Сдвиг тейк-профита не может быть отрицательным"
            )
        tp_offset = (
            price_step * Decimal(self.settings.tp_offset_ticks)
        )
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
        total_steps = int(quantity / quantity_step)
        exact_steps = [
            Decimal(total_steps) * Decimal(share) / Decimal("100")
            for share in shares
        ]
        allocated_steps = [
            int(value.to_integral_value(rounding=ROUND_DOWN))
            for value in exact_steps
        ]
        remaining_steps = total_steps - sum(allocated_steps)
        remainder_priority = sorted(
            range(len(shares)),
            key=lambda index: (
                exact_steps[index] - Decimal(allocated_steps[index]),
                -index,
            ),
            reverse=True,
        )
        for index in remainder_priority[:remaining_steps]:
            allocated_steps[index] += 1
        tp_quantities = [
            Decimal(steps) * quantity_step
            for steps in allocated_steps
        ]
        planned = []
        for index, (raw_price, tp_quantity) in enumerate(
            zip(selected_prices, tp_quantities),
            start=1,
        ):
            normalized_price = Decimal(str(raw_price)).quantize(
                price_step,
                rounding=(
                    ROUND_DOWN
                    if signal.side is OrderSide.LONG
                    else ROUND_UP
                ),
            )
            price = (
                normalized_price - tp_offset
                if signal.side is OrderSide.LONG
                else normalized_price + tp_offset
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
            signal_stop_loss=float(signal_stop),
            max_stop_roi_percent=(
                self.settings.max_stop_roi_percent
            ),
            taker_fee_rate=float(self._taker_fee_rate()),
            limit_price=limit_price,
            trigger_price=trigger_price,
            raw_take_profits=tuple(selected_prices),
            api_execution_supported=instrument.api_supported,
        )
