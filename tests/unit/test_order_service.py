import pytest

from services.order_service import OrderService
from tests.fixtures.bitunix_responses import ORDER_ERROR, ORDER_SUCCESS
from tests.fixtures.bitunix_responses import (
    BATCH_ORDER_PARTIAL,
    CANCEL_ORDERS_SUCCESS,
    PENDING_ORDERS_SUCCESS,
)


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, endpoint, query_params="", body=None):
        self.calls.append((endpoint, query_params, body))
        return self.response

    def get(self, endpoint, query_params=""):
        self.calls.append((endpoint, query_params))
        return self.response


@pytest.mark.parametrize("response", [ORDER_SUCCESS, ORDER_ERROR])
def test_place_order_returns_raw_api_response(response):
    client = FakeClient(response)
    service = OrderService(client)

    result = service.open_long(
        symbol="BTCUSDT",
        quantity=0.01,
        sl_price=59000,
        tp_price=61000,
    )

    assert result.code == response["code"]
    assert result.message == response["msg"]
    assert result.order_id == response["data"].get("orderId")
    assert result.success is (response["code"] == 0)
    endpoint, query_params, body = client.calls[0]
    assert endpoint == "/api/v1/futures/trade/place_order"
    assert query_params == ""
    assert body == {
        "symbol": "BTCUSDT",
        "side": "BUY",
        "orderType": "MARKET",
        "qty": "0.01",
        "tradeSide": "OPEN",
        "effect": "GTC",
        "reduceOnly": False,
        "tpPrice": "61000",
        "tpStopType": "LAST_PRICE",
        "tpOrderType": "MARKET",
        "slPrice": "59000",
        "slStopType": "MARK_PRICE",
        "slOrderType": "MARKET",
    }


def test_open_short_uses_sell_side():
    client = FakeClient(ORDER_SUCCESS)

    OrderService(client).open_short("BTCUSDT", 0.01)

    assert client.calls[0][2]["side"] == "SELL"
    assert client.calls[0][2]["tradeSide"] == "OPEN"


def test_place_batch_orders_parses_partial_result():
    client = FakeClient(BATCH_ORDER_PARTIAL)
    orders = (
        {
            "clientId": "client-1",
            "side": "BUY",
            "qty": "0.6",
        },
        {
            "clientId": "client-2",
            "side": "BUY",
            "qty": "0.4",
        },
    )

    result = OrderService(client).place_batch_orders(
        "BTCUSDT",
        orders,
    )

    assert result.placed[0].order_id == "batch-1"
    assert result.placed[0].client_id == "client-1"
    assert result.failed[0].client_id == "client-2"
    assert result.failed[0].error_code == "10012"
    assert client.calls == [(
        "/api/v1/futures/trade/batch_order",
        "",
        {
            "symbol": "BTCUSDT",
            "orderList": list(orders),
        },
    )]


@pytest.mark.parametrize("orders", ((), ({},) * 6))
def test_place_batch_orders_validates_size(orders):
    with pytest.raises(
        ValueError,
        match="between 1 and 5 orders",
    ):
        OrderService(FakeClient({})).place_batch_orders(
            "BTCUSDT",
            orders,
        )


def test_get_pending_orders_builds_query_and_parses_response():
    client = FakeClient(PENDING_ORDERS_SUCCESS)

    orders = OrderService(client).get_pending_orders(
        symbol="BTCUSDT",
        status="PART_FILLED",
        skip=0,
        limit=25,
    )

    assert len(orders) == 1
    assert orders[0].order_id == "11111"
    assert orders[0].traded_quantity == "0.5"
    assert orders[0].order_type == "LIMIT"
    assert client.calls == [
        (
            "/api/v1/futures/trade/get_pending_orders",
            "skip=0&limit=25&symbol=BTCUSDT&status=PART_FILLED",
        )
    ]


def test_cancel_orders_parses_success_and_failure():
    client = FakeClient(CANCEL_ORDERS_SUCCESS)

    result = OrderService(client).cancel_orders(
        "BTCUSDT",
        ("11111", "11112"),
    )

    assert result.canceled[0].order_id == "11111"
    assert result.failed[0].order_id == "11112"
    assert result.failed[0].error_code == "10013"
    assert result.confirmation_pending is True
    assert client.calls == [
        (
            "/api/v1/futures/trade/cancel_orders",
            "",
            {
                "symbol": "BTCUSDT",
                "orderList": [
                    {"orderId": "11111"},
                    {"orderId": "11112"},
                ],
            },
        )
    ]


@pytest.mark.parametrize(
    ("order_ids", "message"),
    [
        ((), "at least one order_id"),
        (("11111", ""), "order_id cannot be empty"),
    ],
)
def test_cancel_orders_validates_ids(order_ids, message):
    with pytest.raises(ValueError, match=message):
        OrderService(FakeClient({})).cancel_orders(
            "BTCUSDT",
            order_ids,
        )
