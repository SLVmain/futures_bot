from decimal import Decimal
from types import SimpleNamespace

import pytest

from core.api_models import OpenPosition
from core.errors import BitunixResponseError
from services.protection_service import ProtectionService


class FakeClient:
    def get(self, endpoint, query):
        self.call = (endpoint, query)
        return {"code": 0, "data": [
            {
                "positionId": "position-1",
                "tpPrice": "55",
                "slPrice": "",
            }
        ]}


def make_position():
    return OpenPosition(
        position_id="position-1",
        symbol="BTCUSDT",
        quantity="1",
        side="LONG",
        entry_value="50",
        margin_mode="ISOLATION",
        position_mode="ONE_WAY",
        leverage=10,
        margin="5",
        unrealized_pnl="0",
        liquidation_price="20",
        average_open_price="50",
    )


def test_detects_missing_stop_loss():
    client = FakeClient()
    service = ProtectionService(client)

    protections = service.get_pending_tp_sl()
    result = service.unprotected_positions(
        (make_position(),),
        protections,
    )

    assert result[0][1] == ("SL",)
    assert client.call[0] == (
        "/api/v1/futures/tpsl/get_pending_orders"
    )


def test_partial_protection_must_cover_position_quantity():
    service = ProtectionService(None)
    position = make_position()
    protections = (
        {
            "positionId": position.position_id,
            "tpPrice": "55",
            "tpQty": "0.5",
            "slPrice": "45",
            "slQty": "0.5",
        },
        {
            "positionId": position.position_id,
            "tpPrice": "60",
            "tpQty": "0.5",
            "slPrice": "45",
            "slQty": "0.4",
        },
    )

    result = service.unprotected_positions(
        (position,),
        protections,
    )

    assert result == ((position, ("SL",)),)


def test_partial_protection_covering_position_is_accepted():
    service = ProtectionService(None)
    position = make_position()
    protections = (
        {
            "positionId": position.position_id,
            "tpPrice": "55",
            "tpQty": "0.5",
            "slPrice": "45",
            "slQty": "0.5",
        },
        {
            "positionId": position.position_id,
            "tpPrice": "60",
            "tpQty": "0.5",
            "slPrice": "45",
            "slQty": "0.5",
        },
    )

    assert service.unprotected_positions(
        (position,),
        protections,
    ) == ()


def test_fee_aware_break_even_includes_paid_costs():
    long_position = SimpleNamespace(
        average_open_price="50",
        quantity="2",
        side="LONG",
        fee="0.05",
        funding="-0.02",
    )
    short_position = SimpleNamespace(
        average_open_price="50",
        quantity="2",
        side="SHORT",
        fee="0.05",
        funding="-0.02",
    )

    long_price = ProtectionService.fee_aware_break_even(
        long_position,
        2,
        Decimal("0.0006"),
    )
    short_price = ProtectionService.fee_aware_break_even(
        short_position,
        2,
        Decimal("0.0006"),
    )

    assert long_price == Decimal("50.08")
    assert short_price == Decimal("49.92")


def test_take_profit_error_reports_only_response_shape():
    class UnexpectedResponseClient:
        def post(self, *args):
            return {
                "code": 0,
                "data": {
                    "reference": "sensitive-value",
                    "status": "SUCCESS",
                },
            }

    service = ProtectionService(UnexpectedResponseClient())

    with pytest.raises(BitunixResponseError) as captured:
        service.place_take_profit(
            "BTCUSDT",
            "position-1",
            55,
            1,
        )

    message = str(captured.value)
    assert "data type=dict, fields=reference,status" in message
    assert "sensitive-value" not in message


@pytest.mark.parametrize(
    ("data", "order_id"),
    (
        ({"orderId": "order-1"}, "order-1"),
        ({"id": "order-2"}, "order-2"),
        ({"tpSlOrderId": 3}, "3"),
        ([{"id": "order-4"}], "order-4"),
        ("order-5", "order-5"),
    ),
)
def test_take_profit_accepts_supported_order_id_shapes(
    data,
    order_id,
):
    class Client:
        def post(self, *args):
            return {"code": 0, "data": data}

    service = ProtectionService(Client())

    assert service.place_take_profit(
        "BTCUSDT",
        "position-1",
        55,
        1,
    ) == order_id
