from core.api_client import BitunixClient

class AccountService:
    def __init__(self, client: BitunixClient):
        self.client = client
    
    def get_account(self, coin: str = "USDT"):
        endpoint = "/api/v1/futures/account"
        query_params = f"marginCoin={coin}"
        return self.client.get(endpoint, query_params)