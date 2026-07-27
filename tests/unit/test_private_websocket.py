import asyncio
import json
import logging

from services.private_websocket import (
    BitunixPrivateWebSocket,
    WebSocketLoginError,
)


class FakeSocket:
    def __init__(self):
        self.sent = []
        self.messages = [
            json.dumps({"op": "login", "code": 0}),
            json.dumps({
                "ch": "order",
                "ts": 100,
                "data": {
                    "orderId": "1",
                    "orderStatus": "FILLED",
                },
            }),
        ]

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def recv(self):
        return self.messages.pop(0)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.messages:
            raise StopAsyncIteration
        return self.messages.pop(0)


class FakeConnector:
    def __init__(self, socket):
        self.socket = socket

    def __call__(self, url):
        connector = self

        class Context:
            async def __aenter__(self):
                return connector.socket

            async def __aexit__(self, *args):
                return None

        return Context()


def test_websocket_logs_in_subscribes_once_and_dispatches():
    async def scenario():
        socket = FakeSocket()
        received = []
        async def handle(message):
            received.append(message)
            await service.stop()
        service = BitunixPrivateWebSocket(
            "key",
            "secret",
            "wss://example.test/private/",
            handle,
            connector=FakeConnector(socket),
        )

        await service._run_connection()

        assert socket.sent[0]["op"] == "login"
        assert socket.sent[0]["args"][0]["apiKey"] == "key"
        assert socket.sent[1] == {
            "op": "subscribe",
            "args": [
                {"ch": "order"},
                {"ch": "position"},
                {"ch": "balance"},
                {"ch": "tpsl"},
            ],
        }
        assert received[0]["ch"] == "order"

    asyncio.run(scenario())


def test_websocket_disables_library_ping(monkeypatch):
    async def scenario():
        socket = FakeSocket()
        received_options = {}

        def connect(url, **options):
            received_options.update(options)
            return FakeConnector(socket)(url)

        monkeypatch.setattr(
            "websockets.asyncio.client.connect",
            connect,
        )

        async def handle(message):
            await service.stop()

        service = BitunixPrivateWebSocket(
            "key",
            "secret",
            "wss://example.test/private/",
            handle,
        )
        await service._run_connection()

        assert service.ping_interval == 15
        assert received_options == {
            "ping_interval": None,
            "close_timeout": 5,
        }

    asyncio.run(scenario())


def test_websocket_ignores_connect_greeting_before_login():
    async def scenario():
        socket = FakeSocket()
        socket.messages = [
            json.dumps({"op": "connect", "data": "connected"}),
            json.dumps({"op": "login", "code": 0}),
            json.dumps({
                "ch": "order",
                "data": {"orderId": "1"},
            }),
        ]
        received = []

        async def handle(message):
            received.append(message)
            await service.stop()

        service = BitunixPrivateWebSocket(
            "key",
            "secret",
            "wss://example.test/private/",
            handle,
            connector=FakeConnector(socket),
        )

        await service._run_connection()

        assert socket.sent[0]["op"] == "login"
        assert socket.sent[1]["op"] == "subscribe"
        assert received[0]["ch"] == "order"

    asyncio.run(scenario())


def test_login_error_exposes_only_code_and_safe_message():
    async def scenario():
        socket = FakeSocket()
        socket.messages = [json.dumps({
            "op": "login",
            "code": 10004,
            "msg": "IP is not in the whitelist\nTry another IP",
        })]
        service = BitunixPrivateWebSocket(
            "private-key",
            "private-secret",
            "wss://example.test/private/",
            lambda message: None,
            connector=FakeConnector(socket),
        )

        try:
            await service._run_connection()
        except WebSocketLoginError as error:
            assert error.code == "10004"
            assert error.server_message == (
                "IP is not in the whitelist Try another IP"
            )
            assert "private-key" not in str(error)
            assert "private-secret" not in str(error)
            assert "fields=code,msg,op" in str(error)
            assert "op=login" in str(error)
        else:
            raise AssertionError("Login failure was accepted")

    asyncio.run(scenario())


def test_reconnect_reports_login_error_without_credentials(caplog):
    async def scenario():
        socket = FakeSocket()
        socket.messages = [json.dumps({
            "op": "login",
            "code": "10007",
            "msg": "Signature error",
        })]
        statuses = []

        async def status_handler(message):
            statuses.append(message)
            await service.stop()

        service = BitunixPrivateWebSocket(
            "private-key",
            "private-secret",
            "wss://example.test/private/",
            lambda message: None,
            connector=FakeConnector(socket),
            status_handler=status_handler,
        )

        await service.run()

        assert "Код: 10007" in statuses[0]
        assert "Причина: Signature error" in statuses[0]

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())

    assert "code=10007" in caplog.text
    assert "private-key" not in caplog.text
    assert "private-secret" not in caplog.text


def test_login_accepts_event_success_response():
    assert BitunixPrivateWebSocket._is_success(
        {"event": "login", "success": True},
        "login",
    )


def test_login_rejects_event_failure_response():
    assert not BitunixPrivateWebSocket._is_success(
        {"event": "login", "success": False},
        "login",
    )


def test_login_accepts_nested_success_response():
    assert BitunixPrivateWebSocket._is_success(
        {
            "op": "login",
            "data": {"success": True},
        },
        "login",
    )
    assert BitunixPrivateWebSocket._is_success(
        {
            "op": "login",
            "data": {"code": "0"},
        },
        "login",
    )


def test_login_error_reports_nested_shape_without_unknown_values():
    error = WebSocketLoginError(
        None,
        None,
        {
            "op": "login",
            "data": {
                "success": False,
                "accountName": "must-not-be-logged",
            },
        },
    )

    assert "data_fields=accountName,success" in str(error)
    assert "data.success=False" in str(error)
    assert "must-not-be-logged" not in str(error)
