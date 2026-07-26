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
