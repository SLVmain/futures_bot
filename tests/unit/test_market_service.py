from services.market_service import MarketService
from tests.fixtures.bitunix_responses import (
    KLINE_SUCCESS,
    TRADING_PAIRS_SUCCESS,
)


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, endpoint, query_params=""):
        self.calls.append((endpoint, query_params))
        return self.response


def test_get_ticker_parses_documented_kline_response():
    client = FakeClient(KLINE_SUCCESS)
    service = MarketService(client)

    result = service.get_ticker("BTCUSDT")

    assert result.symbol == "BTCUSDT"
    assert result.last_price == 60000.0
    assert client.calls == [
        (
            "/api/v1/futures/market/kline",
            "symbol=BTCUSDT&interval=1m&limit=1",
        )
    ]


def test_get_ticker_returns_none_for_api_error():
    client = FakeClient({"code": 10001, "data": [], "msg": "Invalid request"})

    assert MarketService(client).get_ticker("BTCUSDT") is None


def test_get_trading_pair_parses_limits():
    client = FakeClient(TRADING_PAIRS_SUCCESS)

    pair = MarketService(client).get_trading_pair("BTCUSDT")

    assert pair.min_trade_volume == "0.0001"
    assert pair.base_precision == 4
    assert pair.quote_precision == 1
    assert pair.max_leverage == 125
    assert client.calls == [
        (
            "/api/v1/futures/market/trading_pairs",
            "symbols=BTCUSDT",
        )
    ]
