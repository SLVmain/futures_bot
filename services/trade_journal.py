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
