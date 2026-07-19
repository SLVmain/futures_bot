import json
from core.api_client import BitunixClient

class MarketService:
    def __init__(self, client: BitunixClient):
        self.client = client
    
    def get_ticker(self, symbol: str) -> dict:
        endpoint = "/api/v1/futures/market/kline"
        query_params = f"symbol={symbol}&interval=1m&limit=1"
        
        data = self.client.get(endpoint, query_params)
        
        # Отладка — покажем весь ответ
        # print(f"📦 Полный ответ kline: {json.dumps(data, indent=2)}")
        
        if data.get("code") == 0:
            klines = data.get("data", [])
            # print(f"📦 klines: {klines}")
            
            if klines and len(klines) > 0:
                last = klines[-1] if isinstance(klines, list) else klines
                # print(f"📦 last kline: {last}")
                # print(f"📦 type: {type(last)}")
                
                if isinstance(last, list) and len(last) >= 5:
                    return {
                        "symbol": symbol,
                        "last_price": float(last[4]),
                    }
                elif isinstance(last, dict):
                    # Может быть объектом, а не списком
                    return {
                        "symbol": symbol,
                        "last_price": float(last.get("close", 0)),
                    }
        
        print(f"❌ Не смогли распарсить kline")
        return None