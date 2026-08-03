import re

from models.signal_update import SignalUpdate, SignalUpdateType


class SignalUpdateParser:
    _EVENT_PATTERNS = (
        (
            SignalUpdateType.CLOSE_OPPOSITE,
            r"закрыт[ао]?\s+из-за\s+противоположн(?:ой|ого)\s+сделк",
        ),
        (
            SignalUpdateType.CLOSE_BREAK_EVEN,
            r"идея\s+закрыт[ао]?\s+в\s+безубытк",
        ),
        (
            SignalUpdateType.CLOSE_MARKET,
            r"закрыва(?:ю|ем)\s+идею\s+по\s+текущ",
        ),
        (
            SignalUpdateType.STOP_REPORTED,
            r"идея\s+закрыва(?:ется|ем|ю).*?по\s+стоп",
        ),
        (
            SignalUpdateType.TP1_REPORTED,
            r"(?:достигнут[ао]?\s+первая\s+цель|"
            r"первая\s+цель\s+достигнут[ао]?|"
            r"достигнут\s+перв(?:ый|ого)\s+(?:тейк|цел))",
        ),
        (
            SignalUpdateType.CANCEL_ENTRY,
            r"\bотмена\b",
        ),
    )

    @classmethod
    def parse(cls, message: str) -> SignalUpdate | None:
        if not message or not isinstance(message, str):
            return None
        event_type = next(
            (
                candidate
                for candidate, pattern in cls._EVENT_PATTERNS
                if re.search(pattern, message, re.IGNORECASE | re.DOTALL)
            ),
            None,
        )
        if event_type is None:
            return None
        symbol_match = re.search(
            r"#([A-Z0-9]+(?:/[A-Z0-9]+)?(?:\.P)?)",
            message,
            re.IGNORECASE,
        )
        if symbol_match is None:
            return None
        symbol = cls._normalize_symbol(symbol_match.group(1))
        if not symbol:
            return None
        price_match = re.search(
            r"цена\s+закрытия\s*:\s*\$?([\d.,]+)",
            message,
            re.IGNORECASE,
        )
        percent_match = re.search(
            r"(?:прибыль|результат)?\s*:?\s*"
            r"([+\-−]?\d+(?:[.,]\d+)?)\s*%",
            message,
            re.IGNORECASE,
        )
        return SignalUpdate(
            event_type=event_type,
            symbol=symbol,
            reported_price=(
                price_match.group(1).replace(",", ".")
                if price_match
                else ""
            ),
            reported_percent=(
                percent_match.group(1)
                .replace(",", ".")
                .replace("−", "-")
                if percent_match
                else ""
            ),
            raw_text=message.strip(),
        )

    @staticmethod
    def _normalize_symbol(raw_symbol: str) -> str:
        symbol = raw_symbol.upper().replace("/", "")
        if symbol.endswith(".P"):
            symbol = symbol[:-2]
        if not symbol.endswith(("USDT", "USDC")):
            symbol += "USDT"
        return symbol
