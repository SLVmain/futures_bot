from dataclasses import dataclass
from typing import List
from enum import Enum

class OrderSide(Enum):
    LONG = "LONG"
    SHORT = "SHORT"

@dataclass
class TradeSignal:
    symbol: str
    side: OrderSide
    entry_min: float
    entry_max: float
    take_profits: List[float]
    stop_loss: float
    
    def __str__(self):
        tp_str = "\n".join([f"   TP{i+1}: {tp}" for i, tp in enumerate(self.take_profits)])
        return (
            f"🔔 СИГНАЛ: {self.side.value} {self.symbol}\n"
            f"📥 Вход: {self.entry_min} - {self.entry_max}\n"
            f"🎯 Тейк-профиты:\n{tp_str}\n"
            f"🛑 Стоп-лосс: {self.stop_loss}"
        )