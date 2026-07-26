from urllib.parse import urlencode

from core.api_client import BitunixClient
from core.errors import BitunixResponseError


class ProtectionService:
    def __init__(self, client: BitunixClient):
        self.client = client

    def get_pending_tp_sl(
        self,
        symbol: str | None = None,
        position_id: str | None = None,
    ) -> tuple[dict, ...]:
        parameters = {"skip": 0, "limit": 100}
        if symbol:
            parameters["symbol"] = symbol
        if position_id:
            parameters["positionId"] = position_id
        response = self.client.get(
            "/api/v1/futures/tpsl/get_pending_orders",
            urlencode(parameters),
        )
        data = response.get("data")
        if not isinstance(data, list):
            raise BitunixResponseError(
                "Pending TP/SL data must be a list"
            )
        return tuple(item for item in data if isinstance(item, dict))

    def unprotected_positions(
        self,
        positions,
        protections: tuple[dict, ...],
    ) -> tuple[tuple[object, tuple[str, ...]], ...]:
        result = []
        for position in positions:
            matching = [
                item
                for item in protections
                if str(item.get("positionId", ""))
                == position.position_id
            ]
            missing = []
            if not any(item.get("tpPrice") for item in matching):
                missing.append("TP")
            if not any(item.get("slPrice") for item in matching):
                missing.append("SL")
            if missing:
                result.append((position, tuple(missing)))
        return tuple(result)
