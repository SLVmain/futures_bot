import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable

from core.signature import SignatureGenerator


LOGGER = logging.getLogger(__name__)
EventHandler = Callable[[dict], Awaitable[None]]
StatusHandler = Callable[[str], Awaitable[None]]


class WebSocketLoginError(ConnectionError):
    def __init__(self, code, message, response=None):
        self.code = str(code) if code is not None else "unknown"
        clean_message = " ".join(str(message or "no message").split())
        self.server_message = clean_message[:200]
        response = response if isinstance(response, dict) else {}
        self.response_shape = self._response_shape(response)
        super().__init__(
            "Bitunix WebSocket login failed: "
            f"code={self.code}, message={self.server_message}, "
            f"{self.response_shape}"
        )

    @staticmethod
    def _response_shape(response: dict) -> str:
        fields = ",".join(
            sorted(str(key) for key in response)
        ) or "none"
        markers = []
        for key in ("op", "event", "success"):
            value = response.get(key)
            if isinstance(value, (str, int, float, bool)):
                clean_value = " ".join(str(value).split())[:80]
                markers.append(f"{key}={clean_value}")
        data = response.get("data")
        if isinstance(data, dict):
            data_fields = ",".join(
                sorted(str(key) for key in data)
            ) or "none"
            markers.append(f"data_fields={data_fields}")
            for key in ("code", "success", "result"):
                value = data.get(key)
                if isinstance(value, (str, int, float, bool)):
                    clean_value = " ".join(str(value).split())[:80]
                    markers.append(f"data.{key}={clean_value}")
        marker_text = ", ".join(markers) or "markers=none"
        return f"fields={fields}; {marker_text}"


class BitunixPrivateWebSocket:
    channels = ("order", "position", "balance", "tpsl")

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        url: str,
        event_handler: EventHandler,
        connector=None,
        connected_handler: Callable[[], Awaitable[None]] | None = None,
        status_handler: StatusHandler | None = None,
        ping_interval: float = 15,
        max_backoff: float = 30,
    ):
        self.url = url
        self.signer = SignatureGenerator(api_key, api_secret)
        self.event_handler = event_handler
        self.connector = connector
        self.connected_handler = connected_handler
        self.status_handler = status_handler
        self.ping_interval = ping_interval
        self.max_backoff = max_backoff
        self._stop = asyncio.Event()

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._run_connection()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOGGER.warning(
                    "Bitunix WebSocket disconnected: %s: %s",
                    type(error).__name__,
                    error,
                )
                if self.status_handler is not None:
                    detail = ""
                    if isinstance(error, WebSocketLoginError):
                        detail = (
                            f"\nКод: {error.code}"
                            f"\nПричина: {error.server_message}"
                            f"\nФормат ответа: {error.response_shape}"
                        )
                    await self.status_handler(
                        "⚠️ Соединение Bitunix WebSocket потеряно. "
                        f"Бот переподключается.{detail}"
                    )
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=backoff,
                    )
                except TimeoutError:
                    pass
                backoff = min(backoff * 2, self.max_backoff)

    async def stop(self) -> None:
        self._stop.set()

    async def _run_connection(self) -> None:
        if self.connector is None:
            from websockets.asyncio.client import connect
            connection = connect(
                self.url,
                ping_interval=None,
                close_timeout=5,
            )
        else:
            connection = self.connector(self.url)
        async with connection as socket:
            await socket.send(json.dumps({
                "op": "login",
                "args": [self.signer.generate_websocket()],
            }))
            login_response = await self._receive_login_response(socket)
            if not self._is_success(login_response, "login"):
                login_data = login_response.get("data")
                login_data = (
                    login_data
                    if isinstance(login_data, dict)
                    else {}
                )
                raise WebSocketLoginError(
                    login_response.get(
                        "code",
                        login_data.get("code"),
                    ),
                    login_response.get(
                        "msg",
                        login_response.get(
                            "message",
                            login_data.get(
                                "msg",
                                login_data.get("message"),
                            ),
                        ),
                    ),
                    login_response,
                )
            await socket.send(json.dumps({
                "op": "subscribe",
                "args": [
                    {"ch": channel}
                    for channel in self.channels
                ],
            }))
            if self.connected_handler is not None:
                await self.connected_handler()
            if self.status_handler is not None:
                await self.status_handler(
                    "✅ Мониторинг Bitunix WebSocket подключён"
                )
            ping_task = asyncio.create_task(self._ping(socket))
            try:
                async for raw_message in socket:
                    message = json.loads(raw_message)
                    if message.get("ch") in self.channels:
                        await self.event_handler(message)
            finally:
                ping_task.cancel()
                await asyncio.gather(
                    ping_task,
                    return_exceptions=True,
                )
            if not self._stop.is_set():
                raise ConnectionError(
                    "Bitunix WebSocket stream ended"
                )

    @staticmethod
    async def _receive_login_response(socket) -> dict:
        for _ in range(5):
            raw_message = await asyncio.wait_for(
                socket.recv(),
                timeout=10,
            )
            message = json.loads(raw_message)
            if not isinstance(message, dict):
                continue
            operation = message.get(
                "op",
                message.get("event"),
            )
            if operation == "connect":
                continue
            return message
        raise ConnectionError(
            "Bitunix WebSocket login response was not received"
        )

    async def _ping(self, socket) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.ping_interval)
            await socket.send(json.dumps({
                "op": "ping",
                "ping": int(time.time()),
            }))

    @staticmethod
    def _is_success(message: dict, operation: str) -> bool:
        response_operation = message.get(
            "op",
            message.get("event"),
        )
        if response_operation != operation:
            return False
        if "code" in message:
            return message["code"] in (0, "0")
        if message.get("success") is True:
            return True
        data = message.get("data")
        if isinstance(data, dict):
            if "code" in data:
                return data["code"] in (0, "0")
            return (
                data.get("success") is True
                or data.get("result") is True
            )
        return data is True
