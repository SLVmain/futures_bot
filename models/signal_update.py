from dataclasses import dataclass
from enum import Enum


class SignalUpdateType(str, Enum):
    CANCEL_ENTRY = "SIGNAL_CANCEL"
    CLOSE_MARKET = "SIGNAL_CLOSE_MARKET"
    CLOSE_BREAK_EVEN = "SIGNAL_BREAK_EVEN"
    CLOSE_OPPOSITE = "SIGNAL_CLOSE_OPPOSITE"
    STOP_REPORTED = "SIGNAL_STOP"
    TP1_REPORTED = "SIGNAL_TP1"


@dataclass(frozen=True)
class SignalUpdate:
    event_type: SignalUpdateType
    symbol: str
    reported_price: str = ""
    reported_percent: str = ""
    raw_text: str = ""

    @property
    def requires_position_close(self) -> bool:
        return self.event_type in {
            SignalUpdateType.CANCEL_ENTRY,
            SignalUpdateType.CLOSE_MARKET,
            SignalUpdateType.CLOSE_BREAK_EVEN,
            SignalUpdateType.CLOSE_OPPOSITE,
            SignalUpdateType.STOP_REPORTED,
        }

    @property
    def requires_entry_cancellation(self) -> bool:
        return self.event_type is not SignalUpdateType.TP1_REPORTED
