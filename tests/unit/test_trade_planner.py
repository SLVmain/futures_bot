from dataclasses import FrozenInstanceError

import pytest

from config.settings import TradeSettings
from core.api_models import (
    AccountBalance,
    MarketTicker,
    TradingPair,
)
from models.signal import OrderSide, TradeSignal
from models.trade import TradePlanningError
from services.trade_planner import TradePlanner


class FixedMarket:
    def __init__(self, price, instrument=None):
        self.price = price
        self.calls = 0
        self.instrument = instrument or TradingPair(
            symbol="BTCUSDT",
            min_trade_volume="0.0001",
            max_market_order_volume="50000",
            base_precision=6,
            quote_precision=2,
            min_leverage=1,
            max_leverage=125,
            symbol_status="OPEN",
            api_supported=True,
        )

    def get_ticker(self, symbol):
        self.calls += 1
        return MarketTicker(symbol=symbol, last_price=self.price)

    def get_trading_pair(self, symbol):
        return self.instrument


def test_creates_immutable_trade_plan():
    market = FixedMarket(50)
    planner = TradePlanner(
        market,
        TradeSettings(leverage=10, risk_percent=1),
    )
    signal = TradeSignal(
        symbol="BTCUSDT",
        side=OrderSide.LONG,
        entry_min=49,
        entry_max=51,
        take_profits=[55, 60, 65],
        stop_loss=45,
    )
    account = AccountBalance("USDT", "1000")

    plan = planner.create_plan(signal, account)

    assert plan.symbol == "BTCUSDT"
    assert plan.total_quantity == 2.0
    assert tuple(item.quantity for item in plan.take_profits) == (
        1.0,
        0.6,
        0.4,
    )
    assert tuple(item.price for item in plan.take_profits) == (
        54.98,
        59.98,
        64.98,
    )
    assert plan.risk_budget == 10
    assert plan.api_execution_supported is True
    assert plan.estimated_stop_loss == 10
    assert plan.margin_required == 10
    assert market.calls == 1

    with pytest.raises(FrozenInstanceError):
        plan.total_quantity = 10


def make_signal(
    side=OrderSide.LONG,
    stop_loss=45,
    take_profits=None,
):
    if take_profits is None:
        take_profits = (
            [55, 60, 65]
            if side is OrderSide.LONG
            else [45, 40, 35]
        )
    return TradeSignal(
        symbol="BTCUSDT",
        side=side,
        entry_min=49,
        entry_max=51,
        take_profits=take_profits,
        stop_loss=stop_loss,
    )


def make_planner(
    price=50,
    leverage=10,
    risk_percent=1,
    max_tp_count=3,
    instrument=None,
):
    return TradePlanner(
        FixedMarket(price, instrument),
        TradeSettings(
            leverage=leverage,
            risk_percent=risk_percent,
            max_tp_count=max_tp_count,
        ),
    )


def test_position_size_uses_distance_to_stop_loss():
    account = AccountBalance("USDT", "1000")

    tight_stop = make_planner().create_plan(
        make_signal(stop_loss=49),
        account,
    )
    wide_stop = make_planner().create_plan(
        make_signal(stop_loss=40),
        account,
    )

    assert tight_stop.total_quantity == 10
    assert tight_stop.estimated_stop_loss == 10
    assert wide_stop.total_quantity == 1
    assert wide_stop.estimated_stop_loss == 10


def test_position_size_is_capped_by_available_margin():
    plan = make_planner(leverage=1).create_plan(
        make_signal(stop_loss=49.99),
        AccountBalance("USDT", "1000"),
    )

    assert plan.total_quantity == 20
    assert plan.margin_required == 1000
    assert plan.estimated_stop_loss < plan.risk_budget


def test_position_size_rounds_down_to_six_decimals():
    plan = make_planner().create_plan(
        make_signal(stop_loss=47),
        AccountBalance("USDT", "1000"),
    )

    assert plan.total_quantity == 3.333333
    assert plan.estimated_stop_loss <= plan.risk_budget


def test_short_position_uses_distance_to_stop_loss():
    plan = make_planner().create_plan(
        make_signal(side=OrderSide.SHORT, stop_loss=55),
        AccountBalance("USDT", "1000"),
    )

    assert plan.total_quantity == 2
    assert plan.estimated_stop_loss == 10


def test_single_take_profit_receives_full_quantity():
    plan = make_planner(max_tp_count=1).create_plan(
        make_signal(take_profits=[55]),
        AccountBalance("USDT", "1000"),
    )

    assert [item.quantity for item in plan.take_profits] == [2]


def test_two_take_profits_use_sixty_forty_distribution():
    plan = make_planner(max_tp_count=2).create_plan(
        make_signal(take_profits=[55, 60]),
        AccountBalance("USDT", "1000"),
    )

    assert len(plan.take_profits) == 2
    assert plan.take_profits[0].price == 54.98
    assert [item.quantity for item in plan.take_profits] == [1.2, 0.8]


def test_instrument_precision_rounds_prices_conservatively():
    instrument = TradingPair(
        symbol="BTCUSDT",
        min_trade_volume="0.001",
        max_market_order_volume="100",
        base_precision=3,
        quote_precision=1,
        min_leverage=1,
        max_leverage=20,
        symbol_status="OPEN",
        api_supported=True,
    )
    plan = make_planner(instrument=instrument).create_plan(
        make_signal(
            stop_loss=45.04,
            take_profits=[55.09, 60.09, 65.09],
        ),
        AccountBalance("USDT", "1000"),
    )

    assert plan.stop_loss == 45.1
    assert [item.price for item in plan.take_profits] == [
        54.8,
        59.8,
        64.8,
    ]
    assert sum(
        item.quantity for item in plan.take_profits
    ) == plan.total_quantity


def test_api_unsupported_instrument_still_builds_manual_plan():
    instrument = TradingPair(
        symbol="BTCUSDT",
        min_trade_volume="0.0001",
        max_market_order_volume="50000",
        base_precision=6,
        quote_precision=2,
        min_leverage=1,
        max_leverage=125,
        symbol_status="OPEN",
        api_supported=False,
    )

    plan = make_planner(instrument=instrument).create_plan(
        make_signal(),
        AccountBalance("USDT", "1000"),
    )

    assert plan.api_execution_supported is False
    assert plan.total_quantity == 2
    assert [item.price for item in plan.take_profits] == [
        54.98,
        59.98,
        64.98,
    ]


def test_short_take_profits_are_shifted_two_ticks_higher():
    instrument = TradingPair(
        symbol="BTCUSDT",
        min_trade_volume="0.001",
        max_market_order_volume="100",
        base_precision=3,
        quote_precision=1,
        min_leverage=1,
        max_leverage=20,
        symbol_status="OPEN",
        api_supported=True,
    )

    plan = make_planner(instrument=instrument).create_plan(
        make_signal(
            side=OrderSide.SHORT,
            stop_loss=55,
            take_profits=[45.01, 40.01, 35.01],
        ),
        AccountBalance("USDT", "1000"),
    )

    assert [item.price for item in plan.take_profits] == [
        45.3,
        40.3,
        35.3,
    ]


def test_rejects_leverage_above_instrument_limit():
    instrument = TradingPair(
        symbol="BTCUSDT",
        min_trade_volume="0.001",
        max_market_order_volume="100",
        base_precision=3,
        quote_precision=1,
        min_leverage=1,
        max_leverage=5,
        symbol_status="OPEN",
        api_supported=True,
    )

    with pytest.raises(TradePlanningError, match="Плечо вне диапазона"):
        make_planner(
            leverage=10,
            instrument=instrument,
        ).create_plan(
            make_signal(),
            AccountBalance("USDT", "1000"),
        )


@pytest.mark.parametrize(
    ("side", "stop_loss", "message"),
    [
        (
            OrderSide.LONG,
            51,
            "Для LONG стоп-лосс должен быть ниже",
        ),
        (
            OrderSide.SHORT,
            49,
            "Для SHORT стоп-лосс должен быть выше",
        ),
        (
            OrderSide.LONG,
            50,
            "Расстояние до стоп-лосса не может быть нулевым",
        ),
    ],
)
def test_rejects_invalid_stop_loss_direction(
    side,
    stop_loss,
    message,
):
    with pytest.raises(TradePlanningError, match=message):
        make_planner().create_plan(
            make_signal(side=side, stop_loss=stop_loss),
            AccountBalance("USDT", "1000"),
        )


def test_short_tp_error_contains_tp_and_current_price():
    with pytest.raises(
        TradePlanningError,
        match=(
            r"TP1 расположен.*TP: 55\.02; цена входа: 50; "
            r"текущая цена Bitunix: 50"
        ),
    ):
        make_planner().create_plan(
            make_signal(
                side=OrderSide.SHORT,
                stop_loss=60,
                take_profits=[55],
            ),
            AccountBalance("USDT", "1000"),
        )


def test_outside_range_creates_limit_plan_at_midpoint():
    plan = make_planner(price=60).create_plan(
        make_signal(),
        AccountBalance("USDT", "1000"),
    )

    assert plan.in_range is False
    assert plan.order_type == "LIMIT"
    assert plan.limit_price == 50
    assert plan.planned_entry_price == 50
    assert plan.total_quantity == 2
    assert plan.estimated_stop_loss == 10


@pytest.mark.parametrize(
    ("side", "current_price", "stop_loss", "take_profits"),
    (
        (OrderSide.LONG, 40, 45, [55, 60, 65]),
        (OrderSide.SHORT, 60, 55, [45, 40, 35]),
    ),
)
def test_creates_emulated_trigger_when_limit_would_execute_immediately(
    side,
    current_price,
    stop_loss,
    take_profits,
):
    plan = make_planner(price=current_price).create_plan(
        make_signal(
            side=side,
            stop_loss=stop_loss,
            take_profits=take_profits,
        ),
        AccountBalance("USDT", "1000"),
    )

    assert plan.order_type == "TRIGGER_LIMIT"
    assert plan.trigger_price == 50
    assert plan.limit_price is None
    assert plan.in_range is False


def test_can_disable_emulated_trigger_entries():
    planner = TradePlanner(
        FixedMarket(40),
        TradeSettings(enable_emulated_triggers=False),
    )

    with pytest.raises(TradePlanningError, match="Trigger/Stop-Limit"):
        planner.create_plan(make_signal(), AccountBalance("USDT", "1000"))


def test_short_limit_above_market_remains_allowed():
    plan = make_planner(price=40).create_plan(
        make_signal(
            side=OrderSide.SHORT,
            stop_loss=55,
            take_profits=[45, 40, 35],
        ),
        AccountBalance("USDT", "1000"),
    )

    assert plan.order_type == "LIMIT"
    assert plan.limit_price == 50
    assert plan.limit_price > plan.current_price


def test_inside_range_creates_market_plan():
    plan = make_planner(price=50).create_plan(
        make_signal(),
        AccountBalance("USDT", "1000"),
    )

    assert plan.in_range is True
    assert plan.order_type == "MARKET"
    assert plan.limit_price is None
    assert plan.planned_entry_price == 50


def test_five_take_profits_use_configured_distribution():
    plan = make_planner(max_tp_count=5).create_plan(
        make_signal(take_profits=[55, 60, 65, 70, 75]),
        AccountBalance("USDT", "1000"),
    )

    assert [item.quantity for item in plan.take_profits] == [
        0.8,
        0.5,
        0.3,
        0.2,
        0.2,
    ]
    assert sum(
        item.quantity for item in plan.take_profits
    ) == plan.total_quantity


def test_rounding_remainder_is_distributed_between_take_profits():
    instrument = TradingPair(
        symbol="AAVEUSDT",
        min_trade_volume="0.1",
        max_market_order_volume="50000",
        base_precision=1,
        quote_precision=2,
        min_leverage=1,
        max_leverage=125,
        symbol_status="OPEN",
        api_supported=True,
    )

    plan = make_planner(
        max_tp_count=5,
        instrument=instrument,
    ).create_plan(
        make_signal(take_profits=[55, 60, 65, 70, 75]),
        AccountBalance("USDT", "650"),
    )

    assert plan.total_quantity == 1.3
    assert [item.quantity for item in plan.take_profits] == [
        0.5,
        0.3,
        0.2,
        0.2,
        0.1,
    ]
    assert sum(
        item.quantity for item in plan.take_profits
    ) == plan.total_quantity


def test_suggests_three_targets_when_later_tp_is_below_minimum():
    instrument = TradingPair(
        symbol="BTCUSDT",
        min_trade_volume="0.25",
        max_market_order_volume="50000",
        base_precision=2,
        quote_precision=2,
        min_leverage=1,
        max_leverage=125,
        symbol_status="OPEN",
        api_supported=True,
    )

    with pytest.raises(
        TradePlanningError,
        match="использовать только первые 3 тейк-профита",
    ):
        make_planner(
            max_tp_count=5,
            instrument=instrument,
        ).create_plan(
            make_signal(take_profits=[55, 60, 65, 70, 75]),
            AccountBalance("USDT", "1000"),
        )


@pytest.mark.parametrize(
    ("available", "leverage", "risk_percent", "message"),
    [
        ("0", 10, 1, "Недостаточно средств"),
        ("1000", 0, 1, "Плечо должно быть положительным"),
        (
            "1000",
            10,
            0,
            "Процент риска должен быть положительным",
        ),
    ],
)
def test_rejects_invalid_risk_inputs(
    available,
    leverage,
    risk_percent,
    message,
):
    with pytest.raises(TradePlanningError, match=message):
        make_planner(
            leverage=leverage,
            risk_percent=risk_percent,
        ).create_plan(
            make_signal(),
            AccountBalance("USDT", available),
        )
