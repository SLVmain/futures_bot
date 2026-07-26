import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable

from core.signature import SignatureGenerator


LOGGER = logging.getLogger(__name__)
EventHandler = Callable[[dict], Awaitable[None]]
StatusHandler = Callable[[str], Awaitable[None]]


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
        ping_interval: float = 25,
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
                    "Bitunix WebSocket disconnected: %s",
                    type(error).__name__,
                )
                if self.status_handler is not None:
                    await self.status_handler(
                        "⚠️ Соединение Bitunix WebSocket потеряно. "
                        "Бот переподключается."
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
        connector = self.connector
        if connector is None:
            from websockets.asyncio.client import connect
            connector = connect
        async with connector(self.url) as socket:
            await socket.send(json.dumps({
                "op": "login",
                "args": [self.signer.generate_websocket()],
            }))
            login_response = json.loads(await socket.recv())
            if not self._is_success(login_response, "login"):
                raise ConnectionError("Bitunix WebSocket login failed")
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

    async def _ping(self, socket) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.ping_interval)
            await socket.send(json.dumps({
                "op": "ping",
                "ping": int(time.time()),
            }))

    @staticmethod
    def _is_success(message: dict, operation: str) -> bool:
        if message.get("op") != operation:
            return False
        return message.get("code", 0) in (0, "0")
