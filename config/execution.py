from dataclasses import dataclass
from enum import Enum
from typing import Mapping
from urllib.parse import urlparse


PRODUCTION_BASE_URL = "https://fapi.bitunix.com"


class ExecutionMode(str, Enum):
    DRY_RUN = "dry-run"
    TESTNET = "testnet"
    LIVE = "live"


@dataclass(frozen=True)
class ExecutionConfig:
    mode: ExecutionMode = ExecutionMode.DRY_RUN
    base_url: str = PRODUCTION_BASE_URL

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "ExecutionConfig":
        raw_mode = environ.get("TRADING_MODE", ExecutionMode.DRY_RUN.value)

        try:
            mode = ExecutionMode(raw_mode.strip().lower())
        except ValueError as error:
            allowed = ", ".join(item.value for item in ExecutionMode)
            raise ValueError(
                f"Unknown TRADING_MODE. Expected one of: {allowed}"
            ) from error

        if mode is ExecutionMode.DRY_RUN:
            return cls()

        if mode is ExecutionMode.TESTNET:
            base_url = environ.get("BITUNIX_TESTNET_BASE_URL", "").strip()
            if not base_url:
                raise ValueError(
                    "BITUNIX_TESTNET_BASE_URL is required in testnet mode"
                )
            cls._validate_testnet_url(base_url)
            return cls(mode=mode, base_url=base_url.rstrip("/"))

        return cls(mode=mode, base_url=PRODUCTION_BASE_URL)

    @staticmethod
    def _validate_testnet_url(base_url: str) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError(
                "BITUNIX_TESTNET_BASE_URL must be an absolute HTTPS URL"
            )
        if parsed.hostname == "fapi.bitunix.com":
            raise ValueError(
                "Production Bitunix URL cannot be used in testnet mode"
            )

    @property
    def is_dry_run(self) -> bool:
        return self.mode is ExecutionMode.DRY_RUN
