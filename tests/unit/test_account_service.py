from services.account_service import AccountService
from types import SimpleNamespace

from tests.fixtures.bitunix_responses import (
    ACCOUNT_SUCCESS,
    LEVERAGE_SUCCESS,
)


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, endpoint, query_params=""):
        self.calls.append((endpoint, query_params))
        return self.response


def test_get_account_builds_expected_request():
    client = FakeClient(ACCOUNT_SUCCESS)

    result = AccountService(client).get_account("USDT")

    assert result.margin_coin == "USDT"
    assert result.available == "1000"
    assert result.position_mode == "HEDGE"
    assert client.calls == [
        ("/api/v1/futures/account", "marginCoin=USDT")
    ]


def test_get_account_accepts_legacy_object_data():
    response = {
        "code": 0,
        "data": {
            "marginCoin": "USDT",
            "available": "500",
        },
        "msg": "Success",
    }
    client = FakeClient(response)

    result = AccountService(client).get_account("USDT")

    assert result.available == "500"


class FakeLeverageClient:
    def __init__(self, current_leverage=5, dry_run=False):
        self.current_leverage = current_leverage
        self.execution = SimpleNamespace(is_dry_run=dry_run)
        self.calls = []

    def get(self, endpoint, query_params=""):
        self.calls.append(("GET", endpoint, query_params))
        response = {
            **LEVERAGE_SUCCESS,
            "data": {
                **LEVERAGE_SUCCESS["data"],
                "leverage": self.current_leverage,
            },
        }
        return response

    def post(self, endpoint, query_params="", body=None):
        self.calls.append(("POST", endpoint, query_params, body))
        return {
            "code": 0,
            "data": [
                {
                    "symbol": body["symbol"],
                    "marginCoin": body["marginCoin"],
                    "leverage": body["leverage"],
                }
            ],
            "msg": "Success",
            "simulated": self.execution.is_dry_run,
        }


def test_ensure_leverage_changes_mismatched_live_setting():
    client = FakeLeverageClient(current_leverage=5)

    result = AccountService(client).ensure_leverage(
        "BTCUSDT",
        10,
    )

    assert result.leverage == 10
    assert [call[0] for call in client.calls] == ["GET", "POST"]


def test_ensure_leverage_dry_run_does_not_read_exchange():
    client = FakeLeverageClient(dry_run=True)

    result = AccountService(client).ensure_leverage(
        "BTCUSDT",
        10,
    )

    assert result.simulated is True
    assert [call[0] for call in client.calls] == ["POST"]
