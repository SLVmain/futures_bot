from urllib.parse import urlencode

from core.api_client import BitunixClient
from core.api_models import ClosePositionResult, OpenPosition


class PositionService:
    def __init__(self, client: BitunixClient):
        self.client = client

    def get_open_positions(
        self,
        symbol: str | None = None,
        position_id: str | None = None,
    ) -> tuple[OpenPosition, ...]:
        endpoint = (
            "/api/v1/futures/position/get_pending_positions"
        )
        parameters = {}
        if symbol:
            parameters["symbol"] = symbol
        if position_id:
            parameters["positionId"] = position_id

        response = self.client.get(
            endpoint,
            urlencode(parameters),
        )
        return OpenPosition.list_from_response(response)

    def close_position(
        self,
        position_id: str,
    ) -> ClosePositionResult:
        if not position_id:
            raise ValueError("position_id is required")

        endpoint = (
            "/api/v1/futures/trade/flash_close_position"
        )
        response = self.client.post(
            endpoint,
            "",
            {"positionId": position_id},
        )
        return ClosePositionResult.from_response(response)
