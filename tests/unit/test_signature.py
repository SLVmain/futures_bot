import hashlib

from core.signature import SignatureGenerator


class FixedUuid:
    hex = "fixed-nonce"


def test_generates_expected_signature(monkeypatch):
    monkeypatch.setattr("core.signature.uuid.uuid4", lambda: FixedUuid())
    monkeypatch.setattr("core.signature.time.time", lambda: 1_700_000_000.0)
    generator = SignatureGenerator("test-api-key", "test-api-secret")

    headers = generator.generate("symbolBTCUSDT", '{"symbol":"BTCUSDT"}')

    digest_input = (
        "fixed-nonce"
        "1700000000000"
        "test-api-key"
        "symbolBTCUSDT"
        '{"symbol":"BTCUSDT"}'
    )
    digest = hashlib.sha256(digest_input.encode()).hexdigest()
    expected_sign = hashlib.sha256(
        f"{digest}test-api-secret".encode()
    ).hexdigest()

    assert headers["api-key"] == "test-api-key"
    assert headers["nonce"] == "fixed-nonce"
    assert headers["timestamp"] == "1700000000000"
    assert headers["sign"] == expected_sign
    assert headers["Content-Type"] == "application/json"


def test_generates_expected_websocket_signature(monkeypatch):
    monkeypatch.setattr("core.signature.uuid.uuid4", lambda: FixedUuid())
    monkeypatch.setattr(
        "core.signature.time.time",
        lambda: 1_700_000_000.9,
    )
    generator = SignatureGenerator(
        "test-api-key",
        "test-api-secret",
    )

    result = generator.generate_websocket()

    digest = hashlib.sha256(
        b"fixed-nonce1700000000test-api-key"
    ).hexdigest()
    expected_sign = hashlib.sha256(
        f"{digest}test-api-secret".encode()
    ).hexdigest()
    assert result == {
        "apiKey": "test-api-key",
        "timestamp": 1_700_000_000,
        "nonce": "fixed-nonce",
        "sign": expected_sign,
    }
