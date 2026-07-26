from dataclasses import dataclass
from typing import Mapping

from config.execution import ExecutionMode


@dataclass(frozen=True)
class TelegramAccessConfig:
    allowed_user_ids: frozenset[int]

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str],
        execution_mode: ExecutionMode,
    ) -> "TelegramAccessConfig":
        raw_ids = environ.get(
            "TELEGRAM_ALLOWED_USER_IDS",
            "",
        ).strip()
        if not raw_ids:
            if execution_mode is ExecutionMode.LIVE:
                raise ValueError(
                    "TELEGRAM_ALLOWED_USER_IDS is required "
                    "in live mode"
                )
            return cls(frozenset())

        try:
            user_ids = frozenset(
                int(item.strip())
                for item in raw_ids.split(",")
                if item.strip()
            )
        except ValueError as error:
            raise ValueError(
                "TELEGRAM_ALLOWED_USER_IDS must contain "
                "comma-separated integers"
            ) from error
        if not user_ids:
            raise ValueError(
                "TELEGRAM_ALLOWED_USER_IDS cannot be empty"
            )
        return cls(user_ids)

    def is_allowed(self, user_id: int | None) -> bool:
        if not self.allowed_user_ids:
            return True
        return user_id in self.allowed_user_ids
