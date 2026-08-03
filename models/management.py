from dataclasses import dataclass
from enum import Enum
import time
from uuid import uuid4


class ManagementAction(str, Enum):
    CANCEL_ORDER = "cancel_order"
    CLOSE_POSITION = "close_position"
    CANCEL_ORDERS = "cancel_orders"
    CLOSE_POSITIONS = "close_positions"
    CLOSE_AND_CANCEL = "close_and_cancel"


@dataclass(frozen=True)
class ManagementProposal:
    proposal_id: str
    action: ManagementAction
    symbol: str | None
    target_id: str
    expires_at: float
    order_ids: tuple[str, ...] = ()
    position_ids: tuple[str, ...] = ()
    signal_event_type: str = ""
    source_event_id: str = ""

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

    @classmethod
    def create_signal_action(
        cls,
        action: ManagementAction,
        symbol: str,
        *,
        order_ids: tuple[str, ...] = (),
        position_ids: tuple[str, ...] = (),
        signal_event_type: str,
        source_event_id: str,
        ttl_seconds: int = 300,
        now: float | None = None,
    ) -> "ManagementProposal":
        if not order_ids and not position_ids:
            raise ValueError("Signal action requires at least one target")
        created_at = time.time() if now is None else now
        target_id = position_ids[0] if position_ids else order_ids[0]
        return cls(
            proposal_id=uuid4().hex,
            action=action,
            symbol=symbol,
            target_id=target_id,
            expires_at=created_at + ttl_seconds,
            order_ids=order_ids,
            position_ids=position_ids,
            signal_event_type=signal_event_type,
            source_event_id=source_event_id,
        )

    def is_expired(self, now: float | None = None) -> bool:
        current_time = time.time() if now is None else now
        return current_time >= self.expires_at
