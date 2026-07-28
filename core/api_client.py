import json
import time
from uuid import uuid4
from urllib.parse import parse_qsl

import requests

from config.execution import ExecutionConfig
from .errors import (
    BitunixAPIError,
    BitunixHTTPError,
    BitunixResponseError,
    BitunixTransportError,
)
from .signature import SignatureGenerator

class BitunixClient:
    DEFAULT_TIMEOUT = (5, 15)
    RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        execution: ExecutionConfig = None,
        request_func=None,
        sleep_func=None,
        timeout=None,
        max_get_retries: int = 2,
    ):
        self.sig_gen = SignatureGenerator(api_key, api_secret)
        self.execution = execution or ExecutionConfig()
        self.base_url = self.execution.base_url
        self.request_func = request_func or requests.request
        self.sleep_func = sleep_func or time.sleep
        self.timeout = timeout or self.DEFAULT_TIMEOUT
        self.max_get_retries = max_get_retries
    
    def _request(self, method: str, endpoint: str, query_params: str = "", body: dict = None):
        if method == "POST" and self.execution.is_dry_run:
            return self._dry_run_response(endpoint, body)

        body_str = self._serialize_body(body)
        sign_query = self._canonical_query(query_params)

        url = f"{self.base_url}{endpoint}"
        if query_params:
            url += f"?{query_params}"

        response = self._send_with_retry(
            method=method,
            url=url,
            sign_query=sign_query,
            body_str=body_str,
        )
        result = self._decode_response(response)
        if str(result.get("code")) != "0":
            raise BitunixAPIError(
                result.get("code"),
                result.get("msg", "Unknown API error"),
                result.get("data"),
            )

        return result

    @staticmethod
    def _serialize_body(body: dict = None) -> str:
        if body is None:
            return ""
        return json.dumps(
            body,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    @staticmethod
    def _canonical_query(query_params: str) -> str:
        if not query_params:
            return ""
        pairs = parse_qsl(query_params, keep_blank_values=True)
        pairs.sort(key=lambda item: item[0])
        return "".join(f"{key}{value}" for key, value in pairs)

    def _send_with_retry(
        self,
        method: str,
        url: str,
        sign_query: str,
        body_str: str,
    ):
        max_attempts = 1 + (
            self.max_get_retries if method == "GET" else 0
        )

        for attempt in range(max_attempts):
            headers = self.sig_gen.generate(sign_query, body_str)
            try:
                response = self.request_func(
                    method,
                    url,
                    headers=headers,
                    data=body_str or None,
                    timeout=self.timeout,
                )
            except (requests.Timeout, requests.ConnectionError) as error:
                if attempt + 1 >= max_attempts:
                    raise BitunixTransportError(
                        f"Bitunix request failed: {error}"
                    ) from error
                self.sleep_func(self._retry_delay(attempt, None))
                continue
            except requests.RequestException as error:
                raise BitunixTransportError(
                    f"Bitunix request failed: {error}"
                ) from error

            if (
                response.status_code in self.RETRYABLE_STATUS_CODES
                and attempt + 1 < max_attempts
            ):
                self.sleep_func(self._retry_delay(attempt, response))
                continue

            return response

        raise BitunixTransportError("Bitunix request failed after retries")

    @staticmethod
    def _retry_delay(attempt: int, response) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return min(float(retry_after), 30.0)
                except ValueError:
                    pass
        return min(0.5 * (2 ** attempt), 30.0)

    @staticmethod
    def _decode_response(response) -> dict:
        if not 200 <= response.status_code < 300:
            message = response.text.strip() or "Empty response"
            raise BitunixHTTPError(response.status_code, message[:500])

        try:
            result = response.json()
        except ValueError as error:
            raise BitunixResponseError(
                "Bitunix returned a non-JSON response"
            ) from error

        if not isinstance(result, dict):
            raise BitunixResponseError(
                "Bitunix returned an unexpected JSON structure"
            )
        return result

    @staticmethod
    def _dry_run_response(
        endpoint: str,
        body: dict | None = None,
    ) -> dict:
        if endpoint == "/api/v1/futures/trade/place_order":
            return {
                "code": 0,
                "data": {
                    "orderId": f"dry-run-{uuid4().hex}",
                    "clientId": "",
                },
                "msg": "DRY_RUN: order was not sent to Bitunix",
                "simulated": True,
            }
        if endpoint == "/api/v1/futures/trade/batch_order":
            order_list = (body or {}).get("orderList", [])
            return {
                "code": 0,
                "data": {
                    "successList": [
                        {
                            "id": f"dry-run-{uuid4().hex}",
                            "clientId": order.get("clientId", ""),
                        }
                        for order in order_list
                    ],
                    "failureList": [],
                },
                "msg": (
                    "DRY_RUN: batch orders were not sent to Bitunix"
                ),
                "simulated": True,
            }
        if endpoint == "/api/v1/futures/trade/cancel_orders":
            order_list = (body or {}).get("orderList", [])
            return {
                "code": 0,
                "data": {
                    "successList": order_list,
                    "failureList": [],
                },
                "msg": "DRY_RUN: orders were not canceled on Bitunix",
                "simulated": True,
            }
        if (
            endpoint
            == "/api/v1/futures/trade/flash_close_position"
        ):
            return {
                "code": 0,
                "data": {
                    "positionId": (body or {}).get("positionId"),
                },
                "msg": "DRY_RUN: position was not closed on Bitunix",
                "simulated": True,
            }
        if endpoint == "/api/v1/futures/account/change_leverage":
            request_body = body or {}
            return {
                "code": 0,
                "data": [
                    {
                        "symbol": request_body.get("symbol"),
                        "marginCoin": request_body.get(
                            "marginCoin"
                        ),
                        "leverage": request_body.get("leverage"),
                    }
                ],
                "msg": "DRY_RUN: leverage was not changed on Bitunix",
                "simulated": True,
            }

        return {
            "code": 0,
            "data": {},
            "msg": "DRY_RUN: request was not sent to Bitunix",
            "simulated": True,
        }
    
    def get(self, endpoint: str, query_params: str = ""):
        return self._request("GET", endpoint, query_params)
    
    def post(self, endpoint: str, query_params: str = "", body: dict = None):
        return self._request("POST", endpoint, query_params, body)
