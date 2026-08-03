from dataclasses import dataclass
from pathlib import Path
from decimal import Decimal, InvalidOperation
from typing import Mapping

from config.execution import ExecutionMode


@dataclass(frozen=True)
class MonitoringConfig:
    enabled: bool
    websocket_url: str | None
    journal_path: Path
    taker_fee_rate: Decimal
    auto_break_even_on_tp1: bool

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str],
        mode: ExecutionMode,
    ) -> "MonitoringConfig":
        enabled = (
            environ.get("ENABLE_PRIVATE_WEBSOCKET", "")
            .strip()
            .lower()
            == "true"
        )
        if mode is ExecutionMode.DRY_RUN:
            enabled = False
        elif mode is ExecutionMode.LIVE and not enabled:
            raise ValueError(
                "ENABLE_PRIVATE_WEBSOCKET=true is required "
                "in live mode"
            )

        websocket_url = None
        if enabled:
            if mode is ExecutionMode.LIVE:
                websocket_url = "wss://fapi.bitunix.com/private/"
            else:
                websocket_url = environ.get(
                    "BITUNIX_TESTNET_WEBSOCKET_URL",
                    "",
                ).strip()
                if not websocket_url:
                    raise ValueError(
                        "BITUNIX_TESTNET_WEBSOCKET_URL is required "
                        "when private WebSocket is enabled in testnet"
                    )
                if websocket_url == "wss://fapi.bitunix.com/private/":
                    raise ValueError(
                        "Production WebSocket cannot be used in testnet"
                    )

        path = Path(
            environ.get(
                "TRADE_JOURNAL_PATH",
                "data/trade_journal.csv",
            )
        )
        try:
            taker_fee_rate = Decimal(
                environ.get(
                    "FUTURES_TAKER_FEE_RATE",
                    "0.0006",
                )
            )
        except InvalidOperation as error:
            raise ValueError(
                "FUTURES_TAKER_FEE_RATE must be a decimal"
            ) from error
        if not Decimal("0") <= taker_fee_rate < Decimal("1"):
            raise ValueError(
                "FUTURES_TAKER_FEE_RATE must be between 0 and 1"
            )
        raw_auto_break_even = environ.get(
            "AUTO_BREAK_EVEN_ON_TP1",
            "true",
        ).strip().lower()
        if raw_auto_break_even not in {"true", "false"}:
            raise ValueError(
                "AUTO_BREAK_EVEN_ON_TP1 must be true or false"
            )
        return cls(
            enabled,
            websocket_url,
            path,
            taker_fee_rate,
            raw_auto_break_even == "true",
        )
