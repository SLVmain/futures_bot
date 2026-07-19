import re
from typing import Optional
from models.signal import TradeSignal, OrderSide

class SignalParser:
    @staticmethod
    def parse(message: str) -> Optional[TradeSignal]:
        """Парсит сообщение из Telegram в TradeSignal."""
        try:
            # Ищем символ: #VIRTUALUSDT.P или #BTCUSDT
            symbol_match = re.search(r'#(\S+)', message)
            if not symbol_match:
                return None
            symbol = symbol_match.group(1)
            if symbol.endswith('.P'):
                symbol = symbol[:-2]
            
            # Определяем сторону: ПРОДАЖА/SELL/SHORT или ПОКУПКА/BUY/LONG
            message_upper = message.upper()
            if any(word in message_upper for word in ['ПРОДАЖА', 'SELL', 'SHORT', 'ШОРТ']):
                side = OrderSide.SHORT
            elif any(word in message_upper for word in ['ПОКУПКА', 'BUY', 'LONG', 'ЛОНГ']):
                side = OrderSide.LONG
            else:
                return None
            
            # Парсим диапазон входа (поддержка разных форматов)
            entry_match = re.search(r'Диапазон входа:\s*([\d.]+)\s*-\s*([\d.]+)', message)
            if not entry_match:
                entry_match = re.search(r'Entry:\s*([\d.]+)\s*-\s*([\d.]+)', message)
            if not entry_match:
                return None
            
            entry_min = float(entry_match.group(1))
            entry_max = float(entry_match.group(2))
            
            # Парсим тейк-профиты
            tp_matches = re.findall(r'Тейк-профит\s*\d+:\s*([\d.]+)', message)
            if not tp_matches:
                tp_matches = re.findall(r'Take[- ]?Profit\s*\d+:\s*([\d.]+)', message, re.IGNORECASE)
            
            take_profits = [float(tp) for tp in tp_matches]
            
            # Парсим стоп-лосс
            sl_match = re.search(r'(?:⚠️\s*)?Стоп-лосс:\s*([\d.]+)', message)
            if not sl_match:
                sl_match = re.search(r'Stop[- ]?Loss:\s*([\d.]+)', message, re.IGNORECASE)
            if not sl_match:
                return None
            stop_loss = float(sl_match.group(1))
            
            return TradeSignal(
                symbol=symbol,
                side=side,
                entry_min=entry_min,
                entry_max=entry_max,
                take_profits=take_profits,
                stop_loss=stop_loss
            )
        except Exception as e:
            print(f"Ошибка парсинга: {e}")
            return None