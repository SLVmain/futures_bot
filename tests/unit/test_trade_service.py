from config.settings import TradeSettings
from core.api_models import (
    AccountBalance,
    MarketTicker,
    OrderResult,
    TradingPair,
)
from models.signal import OrderSide, TradeSignal
from services.trade_service import TradeService


class UnusedClient:
    pass


class FixedMarket:
    def __init__(self, price):
        self.price = price
        self.calls = 0

    def get_ticker(self, symbol):
        self.calls += 1
        return MarketTicker(symbol=symbol, last_price=self.price)

    def get_trading_pair(self, symbol):
        return TradingPair(
            symbol=symbol,
            min_trade_volume="0.000001",
            max_market_order_volume="50000",
            base_precision=6,
            quote_precision=6,
            min_leverage=1,
            max_leverage=125,
            symbol_status="OPEN",
            api_supported=True,
        )


class SimulatedOrderService:
    def open_long(self, **kwargs):
        return OrderResult(
            code=0,
            message="DRY_RUN",
            order_id="dry-run-order",
            client_id=None,
            simulated=True,
        )


class FakeAccountService:
    def ensure_leverage(self, symbol, leverage):
        return None


def make_signal():
    return TradeSignal(
        symbol="BTCUSDT",
        side=OrderSide.LONG,
        entry_min=49,
        entry_max=51,
        take_profits=[55, 60, 65],
        stop_loss=45,
    )


def make_account():
    return AccountBalance(
        margin_coin="USDT",
        available="1000",
    )


def test_calculates_position_size_from_stop_loss_risk():
    service = TradeService(
        UnusedClient(),
        TradeSettings(leverage=10, risk_percent=1),
    )
    service.market = FixedMarket(price=50)

    quantity = service.calculate_position_size(
        make_signal(),
        make_account(),
    )

    assert quantity == 2.0


def test_prepare_order_uses_only_first_tp_for_full_quantity():
    service = TradeService(
        UnusedClient(),
        TradeSettings(
            leverage=10,
            risk_percent=1,
            max_tp_count=3,
            tp1_share=0.5,
            tp2_share=0.25,
            tp3_share=0.25,
        ),
    )
    service.market = FixedMarket(price=50)

    result = service.prepare_order(make_signal(), make_account())

    assert result["ready"] is True
    assert result["in_range"] is True
    assert result["total_quantity"] == 2.0
    assert result["take_profits"] == [55.0]
    assert result["tp_quantities"] == [2.0]
    assert result["risk_budget"] == 10
    assert result["estimated_stop_loss"] == 10
    assert service.market.calls == 1


def test_enter_position_propagates_simulated_status():
    service = TradeService(
        UnusedClient(),
        TradeSettings(leverage=10, risk_percent=1),
    )
    service.market = FixedMarket(price=50)
    service.order = SimulatedOrderService()
    service.account = FakeAccountService()

    result = service.enter_position(make_signal(), make_account())

    assert result["success"] is True
    assert result["simulated"] is True
    assert all(order["simulated"] for order in result["orders"])
