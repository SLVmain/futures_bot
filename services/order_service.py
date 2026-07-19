from core.api_client import BitunixClient
import json

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
        tp_stop_type: str = "LAST",
        tp_order_type: str = "MARKET",
        sl_price: float = None,
        sl_stop_type: str = "LAST",
        sl_order_type: str = "MARKET",
    ) -> dict:
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
        
        body_str = json.dumps(data, separators=(',', ':'))
        
        return self.client.post(endpoint, "", data)
    
    def open_long(self, symbol, quantity, price=None, sl_price=None, tp_price=None):
        return self.place_order(
            symbol=symbol,
            side="BUY",
            trade_side="OPEN",
            order_type="LIMIT" if price else "MARKET",
            quantity=quantity,
            price=price,
            sl_price=sl_price,
            tp_price=tp_price,
        )
    
    def open_short(self, symbol, quantity, price=None, sl_price=None, tp_price=None):
        return self.place_order(
            symbol=symbol,
            side="SELL",
            trade_side="OPEN",
            order_type="LIMIT" if price else "MARKET",
            quantity=quantity,
            price=price,
            sl_price=sl_price,
            tp_price=tp_price,
        )