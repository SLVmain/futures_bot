from dataclasses import dataclass
from typing import Mapping

@dataclass
class TradeSettings:
    leverage: int = 10          # Плечо
    risk_percent: float = 1.0   # Процент риска от депозита
    max_tp_count: int = 5       # Сколько тейков использовать (из сигнала)
    tp_offset_ticks: int = 2    # Сдвиг TP к входу в шагах цены
    enable_emulated_triggers: bool = True
    max_stop_roi_percent: float | None = None
    taker_fee_rate: float = 0.0006
    
    # Распределение позиции по тейкам
    tp1_share: float = 0.5      # 50% на первый тейк
    tp2_share: float = 0.25     # 25% на второй
    tp3_share: float = 0.25     # 25% на третий

    @staticmethod
    def take_profit_offset_from_env(
        environ: Mapping[str, str],
    ) -> int:
        try:
            value = int(environ.get("TAKE_PROFIT_OFFSET_TICKS", "2"))
        except ValueError as error:
            raise ValueError(
                "TAKE_PROFIT_OFFSET_TICKS must be an integer"
            ) from error
        if value < 0:
            raise ValueError(
                "TAKE_PROFIT_OFFSET_TICKS must be zero or positive"
            )
        return value
