from decimal import Decimal
from types import SimpleNamespace

from core.api_models import OpenPosition
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
