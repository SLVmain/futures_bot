from dataclasses import dataclass
from typing import Any

from .errors import BitunixResponseError


@dataclass(frozen=True)
class AccountBalance:
    margin_coin: str
    available: str
    frozen: str = "0"
    margin: str = "0"
    transfer: str = "0"
    position_mode: str = ""
    cross_unrealized_pnl: str = "0"
    isolation_unrealized_pnl: str = "0"
    bonus: str = "0"

    @classmethod
    def from_response(
        cls,
        response: dict,
        margin_coin: str,
    ) -> "AccountBalance":
        data = response.get("data")
        if isinstance(data, list):
            account = next(
                (
                    item
                    for item in data
                    if isinstance(item, dict)
                    and item.get("marginCoin") == margin_coin
                ),
                None,
            )
        elif isinstance(data, dict):
            account = data
        else:
            account = None

        if not account or "available" not in account:
            raise BitunixResponseError(
                f"Account data for {margin_coin} is missing"
            )

        return cls(
            margin_coin=str(account.get("marginCoin", margin_coin)),
            available=str(account["available"]),
            frozen=str(account.get("frozen", "0")),
            margin=str(account.get("margin", "0")),
            transfer=str(account.get("transfer", "0")),
            position_mode=str(account.get("positionMode", "")),
            cross_unrealized_pnl=str(
                account.get("crossUnrealizedPNL", "0")
            ),
            isolation_unrealized_pnl=str(
                account.get("isolationUnrealizedPNL", "0")
            ),
            bonus=str(account.get("bonus", "0")),
        )


@dataclass(frozen=True)
class LeverageSettings:
    symbol: str
    margin_coin: str
    leverage: int
    margin_mode: str
    simulated: bool = False

    @classmethod
    def from_response(
        cls,
        response: dict,
        symbol: str,
        margin_coin: str,
    ) -> "LeverageSettings":
        data = response.get("data")
        if isinstance(data, list):
            item = data[0] if data else None
        elif isinstance(data, dict):
            item = data
        else:
            item = None
        if not isinstance(item, dict) or "leverage" not in item:
            raise BitunixResponseError(
                "Leverage settings are missing"
            )
        return cls(
            symbol=str(item.get("symbol", symbol)),
            margin_coin=str(
                item.get("marginCoin", margin_coin)
            ),
            leverage=int(item["leverage"]),
            margin_mode=str(item.get("marginMode", "")),
            simulated=bool(response.get("simulated", False)),
        )


@dataclass(frozen=True)
class MarketTicker:
    symbol: str
    last_price: float

    @classmethod
    def from_kline_response(
        cls,
        response: dict,
        symbol: str,
    ) -> "MarketTicker":
        data = response.get("data")
        if isinstance(data, list) and data:
            latest: Any = data[-1]
        elif isinstance(data, dict):
            latest = data
        else:
            raise BitunixResponseError("Kline data is missing")

        if isinstance(latest, list) and len(latest) >= 5:
            close = latest[4]
        elif isinstance(latest, dict):
            close = latest.get("close")
        else:
            close = None

        try:
            last_price = float(close)
        except (TypeError, ValueError) as error:
            raise BitunixResponseError(
                "Kline close price is invalid"
            ) from error

        if last_price <= 0:
            raise BitunixResponseError(
                "Kline close price must be positive"
            )

        return cls(symbol=symbol, last_price=last_price)


@dataclass(frozen=True)
class TradingPair:
    symbol: str
    min_trade_volume: str
    max_market_order_volume: str
    base_precision: int
    quote_precision: int
    min_leverage: int
    max_leverage: int
    symbol_status: str
    api_supported: bool

    @classmethod
    def from_response(
        cls,
        response: dict,
        symbol: str,
    ) -> "TradingPair":
        data = response.get("data")
        if not isinstance(data, list):
            raise BitunixResponseError(
                "Trading pairs data must be a list"
            )
        pair = next(
            (
                item
                for item in data
                if isinstance(item, dict)
                and item.get("symbol") == symbol
            ),
            None,
        )
        if pair is None:
            raise BitunixResponseError(
                f"Trading pair {symbol} is missing"
            )
        try:
            return cls(
                symbol=str(pair["symbol"]),
                min_trade_volume=str(pair["minTradeVolume"]),
                max_market_order_volume=str(
                    pair["maxMarketOrderVolume"]
                ),
                base_precision=int(pair["basePrecision"]),
                quote_precision=int(pair["quotePrecision"]),
                min_leverage=int(pair["minLeverage"]),
                max_leverage=int(pair["maxLeverage"]),
                symbol_status=str(pair["symbolStatus"]),
                api_supported=bool(pair["isApiSupported"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise BitunixResponseError(
                "Trading pair data is incomplete"
            ) from error


@dataclass(frozen=True)
class OrderResult:
    code: Any
    message: str
    order_id: str | None
    client_id: str | None
    simulated: bool = False

    @classmethod
    def from_response(cls, response: dict) -> "OrderResult":
        data = response.get("data")
        if not isinstance(data, dict):
            data = {}

        return cls(
            code=response.get("code"),
            message=str(response.get("msg", "")),
            order_id=data.get("orderId"),
            client_id=data.get("clientId"),
            simulated=bool(response.get("simulated", False)),
        )

    @property
    def success(self) -> bool:
        return str(self.code) == "0"


@dataclass(frozen=True)
class OpenPosition:
    position_id: str
    symbol: str
    quantity: str
    side: str
    entry_value: str
    margin_mode: str
    position_mode: str
    leverage: int
    margin: str
    unrealized_pnl: str
    liquidation_price: str
    average_open_price: str

    @classmethod
    def list_from_response(
        cls,
        response: dict,
    ) -> tuple["OpenPosition", ...]:
        data = response.get("data")
        if not isinstance(data, list):
            raise BitunixResponseError(
                "Pending positions data must be a list"
            )

        positions = []
        for item in data:
            if not isinstance(item, dict):
                raise BitunixResponseError(
                    "Pending position has an invalid structure"
                )
            position_id = item.get("positionId")
            symbol = item.get("symbol")
            if not position_id or not symbol:
                raise BitunixResponseError(
                    "Pending position identity is missing"
                )
            positions.append(
                cls(
                    position_id=str(position_id),
                    symbol=str(symbol),
                    quantity=str(item.get("qty", "0")),
                    side=str(item.get("side", "")),
                    entry_value=str(item.get("entryValue", "0")),
                    margin_mode=str(item.get("marginMode", "")),
                    position_mode=str(item.get("positionMode", "")),
                    leverage=int(item.get("leverage", 0)),
                    margin=str(item.get("margin", "0")),
                    unrealized_pnl=str(
                        item.get("unrealizedPNL", "0")
                    ),
                    liquidation_price=str(
                        item.get("liqPrice", "0")
                    ),
                    average_open_price=str(
                        item.get("avgOpenPrice", "0")
                    ),
                )
            )
        return tuple(positions)


@dataclass(frozen=True)
class PendingOrder:
    order_id: str
    client_id: str | None
    symbol: str
    quantity: str
    traded_quantity: str
    price: str
    side: str
    order_type: str
    status: str
    reduce_only: bool
    created_at: int
    updated_at: int

    @classmethod
    def list_from_response(
        cls,
        response: dict,
    ) -> tuple["PendingOrder", ...]:
        data = response.get("data")
        if not isinstance(data, dict):
            raise BitunixResponseError(
                "Pending orders data must be an object"
            )
        order_list = data.get("orderList")
        if not isinstance(order_list, list):
            raise BitunixResponseError(
                "Pending order list is missing"
            )

        orders = []
        for item in order_list:
            if not isinstance(item, dict) or not item.get("orderId"):
                raise BitunixResponseError(
                    "Pending order identity is missing"
                )
            orders.append(
                cls(
                    order_id=str(item["orderId"]),
                    client_id=(
                        str(item["clientId"])
                        if item.get("clientId") is not None
                        else None
                    ),
                    symbol=str(item.get("symbol", "")),
                    quantity=str(item.get("qty", "0")),
                    traded_quantity=str(
                        item.get("tradeQty", "0")
                    ),
                    price=str(item.get("price", "0")),
                    side=str(item.get("side", "")),
                    order_type=str(
                        item.get("orderType", item.get("type", ""))
                    ),
                    status=str(item.get("status", "")),
                    reduce_only=bool(item.get("reduceOnly", False)),
                    created_at=int(item.get("ctime", 0)),
                    updated_at=int(item.get("mtime", 0)),
                )
            )
        return tuple(orders)


@dataclass(frozen=True)
class CanceledOrder:
    order_id: str | None
    client_id: str | None


@dataclass(frozen=True)
class CancelOrderFailure:
    order_id: str | None
    client_id: str | None
    error_code: str
    error_message: str


@dataclass(frozen=True)
class CancelOrdersResult:
    canceled: tuple[CanceledOrder, ...]
    failed: tuple[CancelOrderFailure, ...]
    simulated: bool = False
    confirmation_pending: bool = True

    @classmethod
    def from_response(cls, response: dict) -> "CancelOrdersResult":
        data = response.get("data")
        if not isinstance(data, dict):
            raise BitunixResponseError(
                "Cancel orders data must be an object"
            )
        success_list = data.get("successList", [])
        failure_list = data.get("failureList", [])
        if not isinstance(success_list, list) or not isinstance(
            failure_list,
            list,
        ):
            raise BitunixResponseError(
                "Cancel orders lists are invalid"
            )

        return cls(
            canceled=tuple(
                CanceledOrder(
                    order_id=item.get("orderId", item.get("id")),
                    client_id=item.get("clientId"),
                )
                for item in success_list
                if isinstance(item, dict)
            ),
            failed=tuple(
                CancelOrderFailure(
                    order_id=item.get("orderId", item.get("id")),
                    client_id=item.get("clientId"),
                    error_code=str(item.get("errorCode", "")),
                    error_message=str(item.get("errorMsg", "")),
                )
                for item in failure_list
                if isinstance(item, dict)
            ),
            simulated=bool(response.get("simulated", False)),
        )


@dataclass(frozen=True)
class ClosePositionResult:
    position_id: str
    simulated: bool = False
    confirmation_pending: bool = True

    @classmethod
    def from_response(cls, response: dict) -> "ClosePositionResult":
        data = response.get("data")
        if not isinstance(data, dict) or not data.get("positionId"):
            raise BitunixResponseError(
                "Closed position identity is missing"
            )
        return cls(
            position_id=str(data["positionId"]),
            simulated=bool(response.get("simulated", False)),
        )
