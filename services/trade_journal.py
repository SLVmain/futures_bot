import csv
import fcntl
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4


@dataclass(frozen=True)
class JournalEvent:
    event_type: str
    status: str
    symbol: str = ""
    side: str = ""
    order_type: str = ""
    entry_price: str = ""
    quantity: str = ""
    leverage: str = ""
    risk_percent: str = ""
    stop_loss: str = ""
    take_profit: str = ""
    proposal_id: str = ""
    execution_id: str = ""
    user_id: str = ""
    mode: str = ""
    client_id: str = ""
    order_id: str = ""
    position_id: str = ""
    simulated: str = ""
    error: str = ""
    pnl: str = ""
    source_event_id: str = ""
    event_id: str = ""
    timestamp: str = ""

    def as_row(self) -> dict[str, str]:
        row = {
            item.name: str(getattr(self, item.name))
            for item in fields(self)
        }
        row["event_id"] = self.event_id or uuid4().hex
        row["timestamp"] = self.timestamp or datetime.now(
            timezone.utc
        ).isoformat()
        return row


class CsvTradeJournal:
    fieldnames = tuple(item.name for item in fields(JournalEvent))

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = Lock()
        self._seen_event_ids: set[str] = set()
        self._load_seen_event_ids()

    def _load_seen_event_ids(self) -> None:
        if not self.path.exists():
            return
        with self.path.open(
            "r",
            encoding="utf-8",
            newline="",
        ) as stream:
            for row in csv.DictReader(stream):
                event_id = row.get("source_event_id", "")
                if event_id:
                    self._seen_event_ids.add(event_id)

    def append(self, event: JournalEvent) -> bool:
        row = event.as_row()
        deduplication_id = row["source_event_id"]
        with self._lock:
            if (
                deduplication_id
                and deduplication_id in self._seen_event_ids
            ):
                return False
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open(
                "a+",
                encoding="utf-8",
                newline="",
            ) as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    stream.seek(0, 2)
                    write_header = stream.tell() == 0
                    writer = csv.DictWriter(
                        stream,
                        fieldnames=self.fieldnames,
                    )
                    if write_header:
                        writer.writeheader()
                    writer.writerow(row)
                    stream.flush()
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            if deduplication_id:
                self._seen_event_ids.add(deduplication_id)
            return True

    def save_plan(self, client_id: str, plan) -> None:
        for index, take_profit in enumerate(
            plan.take_profits,
            start=1,
        ):
            self.append(JournalEvent(
                event_type="planned_tp",
                status="PENDING",
                symbol=plan.symbol,
                side=plan.side.value,
                order_type=plan.order_type,
                entry_price=str(plan.planned_entry_price),
                quantity=str(take_profit.quantity),
                stop_loss=str(plan.stop_loss),
                take_profit=str(take_profit.price),
                execution_id=plan.execution_id,
                client_id=client_id,
                source_event_id=f"plan:{client_id}:{index}",
            ))

    def finish_plan(self, client_id: str, status: str) -> None:
        self.append(JournalEvent(
            event_type="planned_tp_status",
            status=status,
            client_id=client_id,
            source_event_id=f"plan:{client_id}:{status}",
        ))

    def load_pending_plans(self) -> dict[str, object]:
        if not self.path.exists():
            return {}
        with self.path.open(
            "r",
            encoding="utf-8",
            newline="",
        ) as stream:
            rows = list(csv.DictReader(stream))
        finished = {
            row["client_id"]
            for row in rows
            if row.get("event_type") == "planned_tp_status"
        }
        groups = {}
        for row in rows:
            client_id = row.get("client_id", "")
            if (
                row.get("event_type") == "planned_tp"
                and client_id
                and client_id not in finished
            ):
                groups.setdefault(client_id, []).append(row)

        from models.signal import OrderSide
        from models.trade import PlannedTakeProfit, TradePlan

        plans = {}
        for client_id, plan_rows in groups.items():
            first = plan_rows[0]
            entry = float(first["entry_price"])
            plans[client_id] = TradePlan(
                symbol=first["symbol"],
                side=OrderSide(first["side"]),
                entry_min=entry,
                entry_max=entry,
                current_price=entry,
                in_range=first["order_type"] == "MARKET",
                total_quantity=sum(
                    float(row["quantity"])
                    for row in plan_rows
                ),
                stop_loss=float(first["stop_loss"]),
                take_profits=tuple(
                    PlannedTakeProfit(
                        float(row["take_profit"]),
                        float(row["quantity"]),
                    )
                    for row in plan_rows
                ),
                leverage=0,
                risk_percent=0,
                risk_budget=0,
                limit_price=(
                    entry
                    if first["order_type"] == "LIMIT"
                    else None
                ),
                execution_id=first["execution_id"],
            )
        return plans

    def save_tp_order(
        self,
        order_id: str,
        position_id: str,
        client_id: str,
        tp_number: int,
    ) -> None:
        self.append(JournalEvent(
            event_type="tp_order",
            status=f"TP{tp_number}_ACTIVE",
            client_id=client_id,
            order_id=order_id,
            position_id=position_id,
            source_event_id=f"tp-order:{order_id}",
        ))

    def load_active_tp1_orders(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        with self.path.open(
            "r",
            encoding="utf-8",
            newline="",
        ) as stream:
            rows = list(csv.DictReader(stream))
        completed_positions = {
            row["position_id"]
            for row in rows
            if (
                row.get("event_type") == "break_even"
                and row.get("status") == "COMPLETED"
            )
        }
        return {
            row["order_id"]: row["position_id"]
            for row in rows
            if (
                row.get("event_type") == "tp_order"
                and row.get("status") == "TP1_ACTIVE"
                and row.get("position_id")
                not in completed_positions
            )
        }
