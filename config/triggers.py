from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class TriggerConfig:
    enabled: bool
    state_path: Path
    poll_interval: float
    max_age_seconds: float
    limit_offset_ticks: int
    price_retry_count: int
    price_retry_delay: float
    max_entry_deviation_percent: Decimal
    limit_timeout_seconds: float

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "TriggerConfig":
        raw_enabled = environ.get(
            "ENABLE_EMULATED_ENTRY_TRIGGERS", "true"
        ).strip().lower()
        if raw_enabled not in {"true", "false"}:
            raise ValueError(
                "ENABLE_EMULATED_ENTRY_TRIGGERS must be true or false"
            )
        try:
            poll_interval = float(environ.get("TRIGGER_POLL_SECONDS", "3"))
            max_age_minutes = float(
                environ.get("TRIGGER_MAX_AGE_MINUTES", "1440")
            )
            limit_offset_ticks = int(
                environ.get("TRIGGER_LIMIT_OFFSET_TICKS", "2")
            )
            price_retry_count = int(
                environ.get("TRIGGER_PRICE_RETRY_COUNT", "3")
            )
            price_retry_delay = float(
                environ.get("TRIGGER_PRICE_RETRY_DELAY_SECONDS", "3")
            )
            max_entry_deviation = Decimal(
                environ.get(
                    "TRIGGER_MAX_ENTRY_DEVIATION_PERCENT",
                    "0.15",
                )
            )
            limit_timeout = float(
                environ.get("TRIGGER_LIMIT_TIMEOUT_SECONDS", "30")
            )
        except (ValueError, InvalidOperation) as error:
            raise ValueError(
                "Trigger intervals and offset must be numbers"
            ) from error
        if poll_interval < 1:
            raise ValueError("TRIGGER_POLL_SECONDS must be at least 1")
        if max_age_minutes <= 0:
            raise ValueError("TRIGGER_MAX_AGE_MINUTES must be positive")
        if limit_offset_ticks < 1:
            raise ValueError(
                "TRIGGER_LIMIT_OFFSET_TICKS must be at least 1"
            )
        if price_retry_count < 1:
            raise ValueError("TRIGGER_PRICE_RETRY_COUNT must be at least 1")
        if price_retry_delay < 0:
            raise ValueError(
                "TRIGGER_PRICE_RETRY_DELAY_SECONDS cannot be negative"
            )
        if max_entry_deviation <= 0:
            raise ValueError(
                "TRIGGER_MAX_ENTRY_DEVIATION_PERCENT must be positive"
            )
        if limit_timeout <= 0:
            raise ValueError(
                "TRIGGER_LIMIT_TIMEOUT_SECONDS must be positive"
            )
        return cls(
            enabled=raw_enabled == "true",
            state_path=Path(environ.get(
                "TRIGGER_STATE_PATH", "data/emulated_triggers.json"
            )),
            poll_interval=poll_interval,
            max_age_seconds=max_age_minutes * 60,
            limit_offset_ticks=limit_offset_ticks,
            price_retry_count=price_retry_count,
            price_retry_delay=price_retry_delay,
            max_entry_deviation_percent=max_entry_deviation,
            limit_timeout_seconds=limit_timeout,
        )
