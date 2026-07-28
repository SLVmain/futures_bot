ACCOUNT_SUCCESS = {
    "code": 0,
    "data": [
        {
            "marginCoin": "USDT",
            "available": "1000",
            "frozen": "0",
            "margin": "10",
            "transfer": "1000",
            "positionMode": "HEDGE",
            "crossUnrealizedPNL": "2",
            "isolationUnrealizedPNL": "0",
            "bonus": "0",
        }
    ],
    "msg": "Success",
}

LEVERAGE_SUCCESS = {
    "code": 0,
    "data": {
        "symbol": "BTCUSDT",
        "marginCoin": "USDT",
        "leverage": 10,
        "marginMode": "ISOLATION",
    },
    "msg": "Success",
}

KLINE_SUCCESS = {
    "code": 0,
    "data": [
        {
            "open": 60000,
            "high": 60001,
            "close": 60000,
            "low": 59989.2,
            "time": 111111,
            "quoteVol": "1",
            "baseVol": "60000",
            "type": "LAST_PRICE",
        }
    ],
    "msg": "Success",
}

TRADING_PAIRS_SUCCESS = {
    "code": 0,
    "data": [
        {
            "symbol": "BTCUSDT",
            "base": "BTC",
            "quote": "USDT",
            "minTradeVolume": "0.0001",
            "maxMarketOrderVolume": "50000",
            "basePrecision": 4,
            "quotePrecision": 1,
            "minLeverage": 1,
            "maxLeverage": 125,
            "symbolStatus": "OPEN",
            "isApiSupported": True,
        }
    ],
    "msg": "Success",
}

ORDER_SUCCESS = {
    "code": 0,
    "data": {"orderId": "11111", "clientId": "22222"},
    "msg": "Success",
}

ORDER_ERROR = {
    "code": 10001,
    "data": {},
    "msg": "Invalid request",
}

BATCH_ORDER_PARTIAL = {
    "code": 0,
    "data": {
        "successList": [
            {"id": "batch-1", "clientId": "client-1"},
        ],
        "failureList": [
            {
                "clientId": "client-2",
                "errorCode": "10012",
                "errorMsg": "Insufficient balance",
            },
        ],
    },
    "msg": "Success",
}

OPEN_POSITIONS_SUCCESS = {
    "code": 0,
    "data": [
        {
            "positionId": "12345678",
            "symbol": "BTCUSDT",
            "qty": "0.5",
            "entryValue": "30000",
            "side": "LONG",
            "positionMode": "HEDGE",
            "marginMode": "ISOLATION",
            "leverage": 100,
            "margin": "300",
            "unrealizedPNL": "1.5",
            "liqPrice": "22209",
            "avgOpenPrice": "60000",
        }
    ],
    "msg": "Success",
}

PENDING_ORDERS_SUCCESS = {
    "code": 0,
    "data": {
        "orderList": [
            {
                "orderId": "11111",
                "clientId": "22222",
                "symbol": "BTCUSDT",
                "qty": "1",
                "tradeQty": "0.5",
                "price": "60000",
                "side": "BUY",
                "type": "LIMIT",
                "status": "PART_FILLED",
                "reduceOnly": False,
                "ctime": 1597026383085,
                "mtime": 1597026383086,
            }
        ],
        "total": 1,
    },
    "msg": "Success",
}

CANCEL_ORDERS_SUCCESS = {
    "code": 0,
    "data": {
        "successList": [
            {"orderId": "11111", "clientId": "22222"}
        ],
        "failureList": [
            {
                "orderId": "11112",
                "clientId": "22223",
                "errorMsg": "Order status error",
                "errorCode": 10013,
            }
        ],
    },
    "msg": "Success",
}

CLOSE_POSITION_SUCCESS = {
    "code": 0,
    "data": {"positionId": "12345678"},
    "msg": "Success",
}
