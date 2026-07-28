from urllib.parse import urlencode

from core.api_client import BitunixClient
from core.api_models import (
    BatchOrderResult,
    CancelOrdersResult,
    OrderResult,
    PendingOrder,
)

class OrderService:
    def __init__(self, client: BitunixClient):
        self.client = client
    
    def place_order(
        self,
        symbol: str,
        side: str,
        trade_side: str,
        order_type: str,
        quantity: float,
        price: float = None,
        reduce_only: bool = False,
        effect: str = "GTC",
        tp_price: float = None,
        tp_stop_type: str = "LAST_PRICE",
        tp_order_type: str = "MARKET",
        sl_price: float = None,
        sl_stop_type: str = "LAST_PRICE",
        sl_order_type: str = "MARKET",
        client_id: str = None,
    ) -> OrderResult:
        # Используем place_order вместо batch_order
        endpoint = "/api/v1/futures/trade/place_order"
        
        data = {
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "qty": str(quantity),
            "tradeSide": trade_side,
            "effect": effect,
            "reduceOnly": reduce_only,
        }
        if client_id:
            data["clientId"] = client_id
        
        if price:
            data["price"] = str(price)
        
        if tp_price:
            data["tpPrice"] = str(tp_price)
            data["tpStopType"] = tp_stop_type
            data["tpOrderType"] = tp_order_type
        
        if sl_price:
            data["slPrice"] = str(sl_price)
            data["slStopType"] = sl_stop_type
            data["slOrderType"] = sl_order_type
        
        response = self.client.post(endpoint, "", data)
        return OrderResult.from_response(response)
    
    def open_long(
        self,
        symbol,
        quantity,
        price=None,
        sl_price=None,
        tp_price=None,
        client_id=None,
    ):
        return self.place_order(
            symbol=symbol,
            side="BUY",
            trade_side="OPEN",
            order_type="LIMIT" if price else "MARKET",
            quantity=quantity,
            price=price,
            sl_price=sl_price,
            tp_price=tp_price,
            client_id=client_id,
        )
    
    def open_short(
        self,
        symbol,
        quantity,
        price=None,
        sl_price=None,
        tp_price=None,
        client_id=None,
    ):
        return self.place_order(
            symbol=symbol,
            side="SELL",
            trade_side="OPEN",
            order_type="LIMIT" if price else "MARKET",
            quantity=quantity,
            price=price,
            sl_price=sl_price,
            tp_price=tp_price,
            client_id=client_id,
        )

    def place_batch_orders(
        self,
        symbol: str,
        orders: tuple[dict, ...],
    ) -> BatchOrderResult:
        if not symbol:
            raise ValueError("symbol is required")
        if not 1 <= len(orders) <= 5:
            raise ValueError(
                "batch must contain between 1 and 5 orders"
            )
        response = self.client.post(
            "/api/v1/futures/trade/batch_order",
            "",
            {
                "symbol": symbol,
                "orderList": list(orders),
            },
        )
        return BatchOrderResult.from_response(response)

    def get_pending_orders(
        self,
        symbol: str | None = None,
        order_id: str | None = None,
        client_id: str | None = None,
        status: str | None = None,
        skip: int = 0,
        limit: int = 100,
    ) -> tuple[PendingOrder, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if skip < 0:
            raise ValueError("skip cannot be negative")

        parameters = {"skip": skip, "limit": limit}
        if symbol:
            parameters["symbol"] = symbol
        if order_id:
            parameters["orderId"] = order_id
        if client_id:
            parameters["clientId"] = client_id
        if status:
            parameters["status"] = status

        response = self.client.get(
            "/api/v1/futures/trade/get_pending_orders",
            urlencode(parameters),
        )
        return PendingOrder.list_from_response(response)

    def get_order_detail(
        self,
        client_id: str,
    ) -> dict:
        response = self.client.get(
            "/api/v1/futures/trade/get_order_detail",
            urlencode({"clientId": client_id}),
        )
        data = response.get("data")
        if not isinstance(data, dict):
            raise ValueError("Order detail data must be an object")
        return data

    def cancel_orders(
        self,
        symbol: str,
        order_ids: tuple[str, ...],
    ) -> CancelOrdersResult:
        if not symbol:
            raise ValueError("symbol is required")
        if not order_ids:
            raise ValueError("at least one order_id is required")
        if any(not order_id for order_id in order_ids):
            raise ValueError("order_id cannot be empty")

        body = {
            "symbol": symbol,
            "orderList": [
                {"orderId": order_id}
                for order_id in order_ids
            ],
        }
        response = self.client.post(
            "/api/v1/futures/trade/cancel_orders",
            "",
            body,
        )
        return CancelOrdersResult.from_response(response)
