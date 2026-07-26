from core.api_client import BitunixClient
from core.api_models import AccountBalance, LeverageSettings

class AccountService:
    def __init__(self, client: BitunixClient):
        self.client = client
    
    def get_account(self, coin: str = "USDT") -> AccountBalance:
        endpoint = "/api/v1/futures/account"
        query_params = f"marginCoin={coin}"
        response = self.client.get(endpoint, query_params)
        return AccountBalance.from_response(response, coin)

    def get_leverage(
        self,
        symbol: str,
        margin_coin: str = "USDT",
    ) -> LeverageSettings:
        query_params = (
            f"symbol={symbol}&marginCoin={margin_coin}"
        )
        response = self.client.get(
            "/api/v1/futures/account/get_leverage_margin_mode",
            query_params,
        )
        return LeverageSettings.from_response(
            response,
            symbol,
            margin_coin,
        )

    def change_leverage(
        self,
        symbol: str,
        leverage: int,
        margin_coin: str = "USDT",
    ) -> LeverageSettings:
        if leverage <= 0:
            raise ValueError("leverage must be positive")
        response = self.client.post(
            "/api/v1/futures/account/change_leverage",
            "",
            {
                "symbol": symbol,
                "leverage": leverage,
                "marginCoin": margin_coin,
            },
        )
        return LeverageSettings.from_response(
            response,
            symbol,
            margin_coin,
        )

    def ensure_leverage(
        self,
        symbol: str,
        leverage: int,
        margin_coin: str = "USDT",
    ) -> LeverageSettings:
        if self.client.execution.is_dry_run:
            return self.change_leverage(
                symbol,
                leverage,
                margin_coin,
            )

        current = self.get_leverage(symbol, margin_coin)
        if current.leverage == leverage:
            return current

        changed = self.change_leverage(
            symbol,
            leverage,
            margin_coin,
        )
        if changed.leverage != leverage:
            raise ValueError(
                "Bitunix returned unexpected leverage"
            )
        return changed
