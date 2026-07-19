from dataclasses import dataclass

@dataclass
class TradeSettings:
    leverage: int = 10          # Плечо
    risk_percent: float = 1.0   # Процент риска от депозита
    max_tp_count: int = 3       # Сколько тейков использовать (из сигнала)
    
    # Распределение позиции по тейкам
    tp1_share: float = 0.5      # 50% на первый тейк
    tp2_share: float = 0.25     # 25% на второй
    tp3_share: float = 0.25     # 25% на третий