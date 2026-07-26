import requests
import pytest

from config.execution import ExecutionConfig, ExecutionMode
from core.api_client import BitunixClient
from core.errors import (
    BitunixAPIError,
    BitunixHTTPError,
    BitunixResponseError,
    BitunixTransportError,
)


class FakeResponse:
    def __init__(
        self,
        payload=None,
        status_code=200,
        text="",
        headers=None,
        json_error=None,
    ):
        self.payload = payload
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.json_error = json_error

    def json(self):
        if self.json_error:
            raise self.json_error
        return self.payload


class CapturingSigner:
    def __init__(self):
        self.calls = []

    def generate(self, query_params="", body_str=""):
        self.calls.append((query_params, body_str))
        return {"Content-Type": "application/json"}


def live_config():
    return ExecutionConfig(
        mode=ExecutionMode.LIVE,
        base_url="https://fapi.bitunix.com",
    )


def test_dry_run_post_never_calls_http(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("HTTP request must not run in dry-run mode")

    monkeypatch.setattr(
        "core.api_client.requests.request",
        fail_if_called,
    )
    client = BitunixClient(None, None)

    result = client.post(
        "/api/v1/futures/trade/place_order",
        body={"symbol": "BTCUSDT", "qty": "0.01"},
    )

    assert result["code"] == 0
    assert result["simulated"] is True
    assert result["data"]["orderId"].startswith("dry-run-")
    assert result["msg"] == "DRY_RUN: order was not sent to Bitunix"


def test_dry_run_blocks_unknown_post_endpoint(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("HTTP request must not run in dry-run mode")

    monkeypatch.setattr(
        "core.api_client.requests.request",
        fail_if_called,
    )
    client = BitunixClient(None, None)

    result = client.post(
        "/api/v1/futures/account/change_margin_mode"
    )

    assert result == {
        "code": 0,
        "data": {},
        "msg": "DRY_RUN: request was not sent to Bitunix",
        "simulated": True,
    }


def test_dry_run_simulates_cancel_orders(monkeypatch):
    monkeypatch.setattr(
        "core.api_client.requests.request",
        lambda *args, **kwargs: pytest.fail(
            "HTTP must not run in dry-run"
        ),
    )
    client = BitunixClient(None, None)

    result = client.post(
        "/api/v1/futures/trade/cancel_orders",
        body={
            "symbol": "BTCUSDT",
            "orderList": [{"orderId": "11111"}],
        },
    )

    assert result["simulated"] is True
    assert result["data"]["successList"] == [
        {"orderId": "11111"}
    ]


def test_post_signs_and_sends_the_same_compact_json():
    calls = []

    def request_func(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeResponse(
            {"code": 0, "data": {"orderId": "1"}, "msg": "Success"}
        )

    client = BitunixClient(
        "api-key",
        "api-secret",
        execution=live_config(),
        request_func=request_func,
    )
    signer = CapturingSigner()
    client.sig_gen = signer

    result = client.post(
        "/api/v1/futures/trade/place_order",
        body={"symbol": "BTCUSDT", "qty": "0.01"},
    )

    expected_body = '{"symbol":"BTCUSDT","qty":"0.01"}'
    assert signer.calls == [("", expected_body)]
    assert calls[0][1]["data"] == expected_body
    assert calls[0][1]["timeout"] == (5, 15)
    assert "json" not in calls[0][1]
    assert result["data"]["orderId"] == "1"


def test_get_sorts_query_keys_for_signature():
    def request_func(*args, **kwargs):
        return FakeResponse({"code": 0, "data": {}, "msg": "Success"})

    client = BitunixClient(
        "api-key",
        "api-secret",
        execution=live_config(),
        request_func=request_func,
    )
    signer = CapturingSigner()
    client.sig_gen = signer

    client.get("/endpoint", "symbol=BTCUSDT&interval=1m&limit=1")

    assert signer.calls == [
        ("interval1mlimit1symbolBTCUSDT", "")
    ]


def test_get_retries_timeout_with_backoff():
    attempts = []
    delays = []

    def request_func(*args, **kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise requests.Timeout("timed out")
        return FakeResponse({"code": 0, "data": {}, "msg": "Success"})

    client = BitunixClient(
        "api-key",
        "api-secret",
        execution=live_config(),
        request_func=request_func,
        sleep_func=delays.append,
    )
    signer = CapturingSigner()
    client.sig_gen = signer

    result = client.get("/endpoint")

    assert result["code"] == 0
    assert len(attempts) == 3
    assert delays == [0.5, 1.0]
    assert signer.calls == [("", ""), ("", ""), ("", "")]


def test_get_honors_retry_after_for_retryable_status():
    responses = iter(
        [
            FakeResponse(
                status_code=429,
                text="Rate limited",
                headers={"Retry-After": "2"},
            ),
            FakeResponse({"code": 0, "data": {}, "msg": "Success"}),
        ]
    )
    delays = []
    client = BitunixClient(
        "api-key",
        "api-secret",
        execution=live_config(),
        request_func=lambda *args, **kwargs: next(responses),
        sleep_func=delays.append,
    )

    result = client.get("/endpoint")

    assert result["code"] == 0
    assert delays == [2.0]


def test_post_is_never_retried_after_timeout():
    attempts = []

    def request_func(*args, **kwargs):
        attempts.append(1)
        raise requests.Timeout("unknown submission state")

    client = BitunixClient(
        "api-key",
        "api-secret",
        execution=live_config(),
        request_func=request_func,
        sleep_func=lambda delay: None,
    )

    with pytest.raises(BitunixTransportError):
        client.post("/endpoint", body={"value": 1})

    assert len(attempts) == 1


def test_raises_typed_http_error():
    client = BitunixClient(
        "api-key",
        "api-secret",
        execution=live_config(),
        request_func=lambda *args, **kwargs: FakeResponse(
            status_code=403,
            text="Forbidden",
        ),
    )

    with pytest.raises(BitunixHTTPError) as error:
        client.get("/endpoint")

    assert error.value.status_code == 403


def test_raises_typed_api_error():
    client = BitunixClient(
        "api-key",
        "api-secret",
        execution=live_config(),
        request_func=lambda *args, **kwargs: FakeResponse(
            {"code": 10001, "data": {}, "msg": "Invalid request"}
        ),
    )

    with pytest.raises(BitunixAPIError) as error:
        client.get("/endpoint")

    assert error.value.code == 10001
    assert error.value.message == "Invalid request"


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(json_error=ValueError("invalid JSON")),
        FakeResponse(payload=[]),
    ],
)
def test_raises_typed_response_error(response):
    client = BitunixClient(
        "api-key",
        "api-secret",
        execution=live_config(),
        request_func=lambda *args, **kwargs: response,
    )

    with pytest.raises(BitunixResponseError):
        client.get("/endpoint")
