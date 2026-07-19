from models.signal import TradeSignal, OrderSide
from services.market_service import MarketService
from services.order_service import OrderService
from core.api_client import BitunixClient
from config.settings import TradeSettings

class TradeService:
    def __init__(self, client: BitunixClient, settings: TradeSettings = None):
        self.client = client
        self.market = MarketService(client)
        self.order = OrderService(client)
        self.settings = settings or TradeSettings()
    
    def check_price_in_range(self, signal: TradeSignal) -> dict:
        ticker = self.market.get_ticker(signal.symbol)
        
        if not ticker:
            return {"in_range": False, "error": "Не удалось получить цену", "current_price": None}
        
        current_price = ticker["last_price"]
        entry_min = min(signal.entry_min, signal.entry_max)
        entry_max = max(signal.entry_min, signal.entry_max)
        in_range = entry_min <= current_price <= entry_max
        
        return {
            "in_range": in_range,
            "current_price": current_price,
            "entry_min": entry_min,
            "entry_max": entry_max,
            "side": signal.side.value,
            "symbol": signal.symbol
        }
    
    def calculate_position_size(self, signal: TradeSignal, account_data: dict) -> float:
        """Размер позиции с учётом настроек."""
        available = float(account_data.get("available", 0))
        risk_usdt = available * (self.settings.risk_percent / 100)
        position_usdt = risk_usdt * self.settings.leverage
        
        ticker = self.market.get_ticker(signal.symbol)
        if ticker:
            quantity = position_usdt / ticker["last_price"]
            return round(quantity, 6)
        return 0.0
    
    def prepare_order(self, signal: TradeSignal, account_data: dict) -> dict:
        price_check = self.check_price_in_range(signal)
        
        if price_check.get("error"):
            return {"ready": False, "reason": price_check["error"]}
        
        total_quantity = self.calculate_position_size(signal, account_data)
        
        if total_quantity <= 0:
            return {"ready": False, "reason": "Недостаточно средств"}
        
        # Берём первые N тейков
        tp_count = min(self.settings.max_tp_count, len(signal.take_profits))
        take_profits = signal.take_profits[:tp_count]
        
        # Распределение объёма
        shares = [self.settings.tp1_share, self.settings.tp2_share, self.settings.tp3_share]
        tp_quantities = [round(total_quantity * shares[i], 6) for i in range(tp_count)]
        
        return {
            "ready": True,
            "symbol": signal.symbol,
            "side": signal.side.value,
            "entry_min": price_check["entry_min"],
            "entry_max": price_check["entry_max"],
            "current_price": price_check["current_price"],
            "in_range": price_check["in_range"],
            "total_quantity": total_quantity,
            "stop_loss": signal.stop_loss,
            "take_profits": take_profits,
            "tp_quantities": tp_quantities,
            "leverage": self.settings.leverage,
            "risk_percent": self.settings.risk_percent,
        }
    
    def enter_position(self, signal: TradeSignal, account_data: dict) -> dict:
        order_info = self.prepare_order(signal, account_data)
        
        if not order_info["ready"]:
            return {"success": False, "error": order_info["reason"]}
        
        if not order_info["in_range"]:
            return {"success": False, "error": "Цена вне диапазона"}
        
        sl_price = signal.stop_loss
        take_profits = order_info["take_profits"]
        tp_quantities = order_info["tp_quantities"]
        
        print(f"\n🚀 ВХОД: {signal.side.value} {signal.symbol}")
        print(f"   Плечо: {order_info['leverage']}x | Риск: {order_info['risk_percent']}%")
        print(f"   Общий объём: {order_info['total_quantity']}")
        print(f"   Цена: {order_info['current_price']} | SL: {sl_price}")
        
        orders_placed = []
        
        for i, (tp_price, tp_qty) in enumerate(zip(take_profits, tp_quantities)):
            share_pct = tp_qty / order_info['total_quantity'] * 100
            print(f"   TP{i+1}: {tp_price} | {tp_qty} ({share_pct:.0f}%)")
            
            if signal.side.value == "LONG":
                result = self.order.open_long(
                    symbol=signal.symbol,
                    quantity=tp_qty,
                    price=None,
                    sl_price=sl_price,
                    tp_price=tp_price,
                )
            else:
                result = self.order.open_short(
                    symbol=signal.symbol,
                    quantity=tp_qty,
                    price=None,
                    sl_price=sl_price,
                    tp_price=tp_price,
                )
            
            if result.get("code") == 0:
                data = result.get("data", {})
                order_id = data.get("orderId", "unknown")
                print(f"   ✅ ID: {order_id}")
                orders_placed.append({"tp": i+1, "price": tp_price, "qty": tp_qty, "id": order_id})
            else:
                print(f"   ❌ {result.get('msg')}")
        
        if orders_placed:
            print(f"\n✅ Размещено: {len(orders_placed)} ордеров")
            return {"success": True, "orders": orders_placed, "stop_loss": sl_price}
        
        return {"success": False, "error": "Не удалось разместить ордера"}