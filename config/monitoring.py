from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from config.execution import ExecutionMode


@dataclass(frozen=True)
class MonitoringConfig:
    enabled: bool
    websocket_url: str | None
    journal_path: Path

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
        return cls(enabled, websocket_url, path)
