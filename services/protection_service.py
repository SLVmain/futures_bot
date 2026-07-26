from urllib.parse import urlencode
from decimal import Decimal, ROUND_DOWN, ROUND_UP

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

    def place_take_profit(
        self,
        symbol: str,
        position_id: str,
        price: float,
        quantity: float,
    ) -> str:
        response = self.client.post(
            "/api/v1/futures/tpsl/place_order",
            "",
            {
                "symbol": symbol,
                "positionId": position_id,
                "tpPrice": str(price),
                "tpStopType": "LAST_PRICE",
                "tpOrderType": "MARKET",
                "tpQty": str(quantity),
            },
        )
        data = response.get("data")
        if not isinstance(data, dict) or not data.get("orderId"):
            raise BitunixResponseError(
                "TP order identity is missing"
            )
        return str(data["orderId"])

    def modify_stop_loss(
        self,
        order_id: str,
        price: str,
        quantity: str,
    ) -> str:
        response = self.client.post(
            "/api/v1/futures/tpsl/modify_order",
            "",
            {
                "orderId": order_id,
                "slPrice": price,
                "slStopType": "LAST_PRICE",
                "slOrderType": "MARKET",
                "slQty": quantity,
            },
        )
        data = response.get("data")
        if not isinstance(data, dict) or not data.get("orderId"):
            raise BitunixResponseError(
                "Modified SL identity is missing"
            )
        return str(data["orderId"])

    @staticmethod
    def fee_aware_break_even(
        position,
        quote_precision: int,
        taker_fee_rate: Decimal,
    ) -> Decimal:
        entry = Decimal(position.average_open_price)
        quantity = Decimal(position.quantity)
        if quantity <= 0:
            raise ValueError("Position quantity must be positive")
        paid_fee = abs(Decimal(position.fee))
        funding = Decimal(position.funding)
        paid_funding = max(-funding, Decimal("0"))
        accumulated_cost = paid_fee + paid_funding
        tick = Decimal("1").scaleb(-quote_precision)

        if position.side == "LONG":
            raw_price = (
                entry * quantity + accumulated_cost
            ) / (quantity * (Decimal("1") - taker_fee_rate))
            return (
                raw_price.quantize(tick, rounding=ROUND_UP)
                + tick
            )
        if position.side == "SHORT":
            raw_price = (
                entry * quantity - accumulated_cost
            ) / (quantity * (Decimal("1") + taker_fee_rate))
            return (
                raw_price.quantize(tick, rounding=ROUND_DOWN)
                - tick
            )
        raise ValueError("Unknown position side")

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
