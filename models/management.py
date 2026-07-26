from dataclasses import dataclass
from enum import Enum
import time
from uuid import uuid4


class ManagementAction(str, Enum):
    CANCEL_ORDER = "cancel_order"
    CLOSE_POSITION = "close_position"


@dataclass(frozen=True)
class ManagementProposal:
    proposal_id: str
    action: ManagementAction
    symbol: str | None
    target_id: str
    expires_at: float

    @classmethod
    def create(
        cls,
        action: ManagementAction,
        target_id: str,
        symbol: str | None = None,
        ttl_seconds: int = 300,
        now: float | None = None,
    ) -> "ManagementProposal":
        created_at = time.time() if now is None else now
        return cls(
            proposal_id=uuid4().hex,
            action=action,
            symbol=symbol,
            target_id=target_id,
            expires_at=created_at + ttl_seconds,
        )

    def is_expired(self, now: float | None = None) -> bool:
        current_time = time.time() if now is None else now
        return current_time >= self.expires_at
