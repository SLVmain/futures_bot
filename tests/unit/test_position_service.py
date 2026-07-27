import pytest

from core.api_client import BitunixClient
from core.api_models import OpenPosition
from services.position_service import PositionService
from tests.fixtures.bitunix_responses import (
    CLOSE_POSITION_SUCCESS,
    OPEN_POSITIONS_SUCCESS,
)


class FakeClient:
    def __init__(self):
        self.get_calls = []
        self.post_calls = []

    def get(self, endpoint, query_params=""):
        self.get_calls.append((endpoint, query_params))
        return OPEN_POSITIONS_SUCCESS

    def post(self, endpoint, query_params="", body=None):
        self.post_calls.append((endpoint, query_params, body))
        return CLOSE_POSITION_SUCCESS


def test_get_open_positions_builds_query_and_parses_response():
    client = FakeClient()

    positions = PositionService(client).get_open_positions(
        symbol="BTCUSDT",
        position_id="12345678",
    )

    assert len(positions) == 1
    assert positions[0].position_id == "12345678"
    assert positions[0].symbol == "BTCUSDT"
    assert positions[0].quantity == "0.5"
    assert positions[0].average_open_price == "60000"
    assert positions[0].side == "LONG"
    assert client.get_calls == [
        (
            "/api/v1/futures/position/get_pending_positions",
            "symbol=BTCUSDT&positionId=12345678",
        )
    ]


@pytest.mark.parametrize(
    ("api_side", "position_side"),
    (
        ("BUY", "LONG"),
        ("LONG", "LONG"),
        ("SELL", "SHORT"),
        ("SHORT", "SHORT"),
    ),
)
def test_open_position_normalizes_exchange_side(
    api_side,
    position_side,
):
    response = {
        "data": [{
            "positionId": "position-1",
            "symbol": "BTCUSDT",
            "side": api_side,
        }],
    }

    position = OpenPosition.list_from_response(response)[0]

    assert position.side == position_side


def test_close_position_posts_only_position_id():
    client = FakeClient()

    result = PositionService(client).close_position("12345678")

    assert result.position_id == "12345678"
    assert result.simulated is False
    assert result.confirmation_pending is True
    assert client.post_calls == [
        (
            "/api/v1/futures/trade/flash_close_position",
            "",
            {"positionId": "12345678"},
        )
    ]


def test_close_position_requires_id():
    with pytest.raises(ValueError, match="position_id is required"):
        PositionService(FakeClient()).close_position("")


def test_close_position_is_simulated_by_default_client(monkeypatch):
    monkeypatch.setattr(
        "core.api_client.requests.request",
        lambda *args, **kwargs: pytest.fail(
            "HTTP must not run in dry-run"
        ),
    )
    client = BitunixClient(None, None)

    result = PositionService(client).close_position("12345678")

    assert result.position_id == "12345678"
    assert result.simulated is True
