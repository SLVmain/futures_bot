from core.api_client import BitunixClient
from core.api_models import MarketTicker, TradingPair
from core.errors import BitunixResponseError

class MarketService:
    def __init__(self, client: BitunixClient):
        self.client = client
    
    def get_ticker(self, symbol: str) -> MarketTicker | None:
        endpoint = "/api/v1/futures/market/kline"
        query_params = f"symbol={symbol}&interval=1m&limit=1"
        
        data = self.client.get(endpoint, query_params)

        try:
            return MarketTicker.from_kline_response(data, symbol)
        except BitunixResponseError:
            print("❌ Не смогли распарсить kline")
            return None

    def get_trading_pair(self, symbol: str) -> TradingPair:
        response = self.client.get(
            "/api/v1/futures/market/trading_pairs",
            f"symbols={symbol}",
        )
        return TradingPair.from_response(response, symbol)
