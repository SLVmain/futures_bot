import re
from typing import Optional
from models.signal import TradeSignal, OrderSide

class SignalParser:
    @staticmethod
    def parse(message: str) -> Optional[TradeSignal]:
        """Парсит сообщение из Telegram в TradeSignal."""
        try:
            # Ищем символ: #VIRTUALUSDT.P или #LTC/USDT.P или #BTCUSDT
            symbol_match = re.search(r'#(\S+)', message)
            if not symbol_match:
                return None
            symbol = symbol_match.group(1)
            # Убираем .P и /
            if symbol.endswith('.P'):
                symbol = symbol[:-2]
            symbol = symbol.replace('/', '')
            # LTCUSDTP -> LTCUSDT (убираем одиночную P в конце)
            if symbol.endswith('P') and not symbol.endswith('PP'):
                symbol = symbol[:-1]
            
            # Определяем сторону
            message_upper = message.upper()
            if any(word in message_upper for word in ['ПРОДАЖА', 'SELL', 'SHORT', 'ШОРТ']):
                side = OrderSide.SHORT
            elif any(word in message_upper for word in ['ПОКУПКА', 'BUY', 'LONG', 'ЛОНГ']):
                side = OrderSide.LONG
            else:
                return None
            
            # --- Парсим диапазон входа ---
            # Старый формат: Диапазон входа: 0.81238-0.80422
            entry_match = re.search(r'Диапазон входа:\s*\$?([\d.,]+)\s*[-–—]\s*\$?([\d.,]+)', message)
            if not entry_match:
                # Английский: Entry: 0.5-0.6
                entry_match = re.search(r'Entry:\s*\$?([\d.,]+)\s*[-–—]\s*\$?([\d.,]+)', message)
            if not entry_match:
                return None
            
            entry_1 = float(entry_match.group(1).replace(',', '.'))
            entry_2 = float(entry_match.group(2).replace(',', '.'))
            entry_min = min(entry_1, entry_2)
            entry_max = max(entry_1, entry_2)
            
            # --- Парсим тейк-профиты ---
            # Старый формат: Тейк-профит 1: 0.82462
            tp_matches = re.findall(r'Тейк-профит\s*\d+:\s*([\d.]+)', message)
            if not tp_matches:
                tp_matches = re.findall(r'Take[- ]?Profit\s*\d+:\s*([\d.]+)', message, re.IGNORECASE)
            if not tp_matches:
                # Новый формат: Цели: $48,4 / $50,9 / $58,9
                goals_match = re.search(r'(?:Цели|Goals|Targets)[:\s]*(.+)', message, re.IGNORECASE)
                if goals_match:
                    goals_text = goals_match.group(1)
                    tp_matches = re.findall(r'[\d.]+', goals_text.replace(',', '.'))
            
            take_profits = [float(tp.replace(',', '.')) for tp in tp_matches]
            
            # --- Парсим стоп-лосс ---
            stop_loss = None
            
            # Старый формат: Стоп-лосс: 0.7675
            sl_match = re.search(r'(?:⚠️\s*)?Стоп-лосс:\s*\$?([\d.]+)', message)
            if not sl_match:
                sl_match = re.search(r'Stop[- ]?Loss:\s*\$?([\d.]+)', message, re.IGNORECASE)
            if sl_match:
                stop_loss = float(sl_match.group(1).replace(',', '.'))
            
            # Новый формат: Диапазон стопа: $45,00-44,50
            if stop_loss is None:
                sl_range_match = re.search(r'(?:Диапазон стопа|Stop range)[:\s]*\$?([\d.,]+)\s*[-–—]\s*\$?([\d.,]+)', message, re.IGNORECASE)
                if sl_range_match:
                    sl_1 = float(sl_range_match.group(1).replace(',', '.'))
                    sl_2 = float(sl_range_match.group(2).replace(',', '.'))
                    # Дальний стоп по направлению возможного убытка.
                    if side == OrderSide.LONG:
                        stop_loss = min(sl_1, sl_2)
                    else:
                        stop_loss = max(sl_1, sl_2)
            
            if stop_loss is None:
                return None
            
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
