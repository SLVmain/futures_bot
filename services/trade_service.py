from config.settings import TradeSettings
from core.api_client import BitunixClient
from core.api_models import AccountBalance
from models.signal import TradeSignal
from models.trade import (
    TradeExecutionResult,
    TradePlan,
    TradePlanningError,
)
from services.execution_service import ExecutionService
from services.account_service import AccountService
from services.market_service import MarketService
from services.order_service import OrderService
from services.trade_planner import TradePlanner


class TradeService:
    """Compatibility facade around planning and execution services."""

    def __init__(
        self,
        client: BitunixClient,
        settings: TradeSettings = None,
    ):
        self.client = client
        self.market = MarketService(client)
        self.order = OrderService(client)
        self.account = AccountService(client)
        self.settings = settings or TradeSettings()

    def _planner(self) -> TradePlanner:
        return TradePlanner(self.market, self.settings)

    def build_plan(
        self,
        signal: TradeSignal,
        account: AccountBalance,
    ) -> TradePlan:
        return self._planner().create_plan(signal, account)

    def execute_plan(
        self,
        plan: TradePlan,
    ) -> TradeExecutionResult:
        return ExecutionService(
            self.order,
            self.account,
        ).execute(plan)

    def check_price_in_range(self, signal: TradeSignal) -> dict:
        ticker = self.market.get_ticker(signal.symbol)
        if not ticker:
            return {
                "in_range": False,
                "error": "Не удалось получить цену",
                "current_price": None,
            }

        entry_min = min(signal.entry_min, signal.entry_max)
        entry_max = max(signal.entry_min, signal.entry_max)
        return {
            "in_range": entry_min <= ticker.last_price <= entry_max,
            "current_price": ticker.last_price,
            "entry_min": entry_min,
            "entry_max": entry_max,
            "side": signal.side.value,
            "symbol": signal.symbol,
        }

    def calculate_position_size(
        self,
        signal: TradeSignal,
        account: AccountBalance,
    ) -> float:
        return self._planner().calculate_position_size(
            signal,
            account,
        )

    def prepare_order(
        self,
        signal: TradeSignal,
        account: AccountBalance,
    ) -> dict:
        try:
            return self.build_plan(signal, account).to_order_info()
        except TradePlanningError as error:
            return {"ready": False, "reason": str(error)}

    def enter_position(
        self,
        signal: TradeSignal,
        account: AccountBalance,
    ) -> dict:
        try:
            plan = self.build_plan(signal, account)
        except TradePlanningError as error:
            return {"success": False, "error": str(error)}
        return self.execute_plan(plan).to_dict()
