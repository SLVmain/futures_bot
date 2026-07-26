import asyncio
import json

from services.private_websocket import BitunixPrivateWebSocket


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
