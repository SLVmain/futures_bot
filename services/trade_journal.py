import csv
import fcntl
from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation
from io import StringIO
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
    fee: str = ""
    funding: str = ""
    net_pnl: str = ""
    remaining_quantity: str = ""
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


@dataclass(frozen=True)
class TradeStatistics:
    total: int
    wins: int
    losses: int
    breakeven: int
    realized_pnl: Decimal
    fees: Decimal
    funding: Decimal
    net_pnl: Decimal

    @property
    def win_rate(self) -> Decimal:
        if self.total == 0:
            return Decimal("0")
        return (
            Decimal(self.wins)
            / Decimal(self.total)
            * Decimal("100")
        )


class CsvTradeJournal:
    fieldnames = tuple(item.name for item in fields(JournalEvent))

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = Lock()
        self._seen_event_ids: set[str] = set()
        self._ensure_schema()
        self._load_seen_event_ids()

    def _ensure_schema(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        with self.path.open(
            "r",
            encoding="utf-8",
            newline="",
        ) as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                reader = csv.DictReader(stream)
                existing_fields = tuple(reader.fieldnames or ())
                missing_fields = tuple(
                    name
                    for name in self.fieldnames
                    if name not in existing_fields
                )
                if not missing_fields:
                    self.fieldnames = existing_fields
                    return
                rows = list(reader)
                merged_fields = existing_fields + missing_fields
                temporary_path = self.path.with_suffix(
                    f"{self.path.suffix}.schema.tmp"
                )
                with temporary_path.open(
                    "w",
                    encoding="utf-8",
                    newline="",
                ) as target:
                    writer = csv.DictWriter(
                        target,
                        fieldnames=merged_fields,
                    )
                    writer.writeheader()
                    writer.writerows(rows)
                    target.flush()
                temporary_path.replace(self.path)
                self.fieldnames = merged_fields
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

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
        take_profit: str = "",
    ) -> None:
        self.append(JournalEvent(
            event_type="tp_order",
            status=f"TP{tp_number}_ACTIVE",
            client_id=client_id,
            order_id=order_id,
            position_id=position_id,
            take_profit=take_profit,
            source_event_id=f"tp-order:{order_id}",
        ))

    def load_tp_order_prices(self) -> dict[str, Decimal]:
        if not self.path.exists():
            return {}
        prices = {}
        for row in self._read_rows():
            if row.get("event_type") != "tp_order":
                continue
            try:
                price = Decimal(row.get("take_profit", ""))
            except InvalidOperation:
                continue
            if row.get("order_id") and price > 0:
                prices[row["order_id"]] = price
        return prices

    def load_active_tp1_orders(self) -> dict[str, str]:
        return {
            order_id: position_id
            for order_id, (position_id, tp_number)
            in self.load_active_tp_orders().items()
            if tp_number == 1
        }

    def load_active_tp_order_client_ids(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        with self.path.open(
            "r",
            encoding="utf-8",
            newline="",
        ) as stream:
            rows = list(csv.DictReader(stream))
        return {
            row["order_id"]: row.get("client_id", "")
            for row in rows
            if (
                row.get("event_type") == "tp_order"
                and row.get("order_id")
                and row.get("client_id")
            )
        }

    def load_active_tp_orders(
        self,
    ) -> dict[str, tuple[str, int]]:
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
        result = {}
        for row in rows:
            status = row.get("status", "")
            if (
                row.get("event_type") != "tp_order"
                or not status.startswith("TP")
                or not status.endswith("_ACTIVE")
                or row.get("position_id") in completed_positions
            ):
                continue
            try:
                tp_number = int(status[2:-7])
            except ValueError:
                continue
            result[row["order_id"]] = (
                row["position_id"],
                tp_number,
            )
        return result

    def load_trade_summaries(
        self,
        limit: int | None = None,
    ) -> tuple[dict[str, str], ...]:
        rows = [
            row
            for row in self._read_rows()
            if row.get("event_type") == "TRADE_SUMMARY"
        ]
        if limit is not None:
            if limit < 0:
                raise ValueError("limit cannot be negative")
            rows = rows[-limit:] if limit else []
        return tuple(reversed(rows))

    def trade_statistics(self) -> TradeStatistics:
        summaries = self.load_trade_summaries()
        net_values = [
            value
            for row in summaries
            if (value := self._decimal(row.get("net_pnl"))) is not None
        ]
        return TradeStatistics(
            total=len(summaries),
            wins=sum(value > 0 for value in net_values),
            losses=sum(value < 0 for value in net_values),
            breakeven=sum(value == 0 for value in net_values),
            realized_pnl=self._sum_column(summaries, "pnl"),
            fees=sum(
                (
                    abs(value)
                    for row in summaries
                    if (value := self._decimal(row.get("fee")))
                    is not None
                ),
                Decimal("0"),
            ),
            funding=self._sum_column(summaries, "funding"),
            net_pnl=sum(net_values, Decimal("0")),
        )

    def csv_snapshot(self) -> bytes:
        rows = self._read_rows()
        output = StringIO(newline="")
        writer = csv.DictWriter(
            output,
            fieldnames=self.fieldnames,
        )
        writer.writeheader()
        writer.writerows(rows)
        return output.getvalue().encode("utf-8")

    def _read_rows(self) -> list[dict[str, str]]:
        with self._lock:
            if not self.path.exists():
                return []
            with self.path.open(
                "r",
                encoding="utf-8",
                newline="",
            ) as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
                try:
                    return list(csv.DictReader(stream))
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @classmethod
    def _sum_column(
        cls,
        rows: tuple[dict[str, str], ...],
        column: str,
    ) -> Decimal:
        return sum(
            (
                value
                for row in rows
                if (value := cls._decimal(row.get(column))) is not None
            ),
            Decimal("0"),
        )

    @staticmethod
    def _decimal(value) -> Decimal | None:
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError):
            return None
