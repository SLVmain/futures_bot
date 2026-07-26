import csv

from models.signal import OrderSide
from models.trade import PlannedTakeProfit, TradePlan
from services.trade_journal import CsvTradeJournal, JournalEvent


def test_journal_writes_header_and_rows(tmp_path):
    path = tmp_path / "journal.csv"
    journal = CsvTradeJournal(path)

    assert journal.append(JournalEvent(
        event_type="execution",
        status="SUCCESS",
        symbol="BTCUSDT",
        source_event_id="event-1",
    ))

    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTCUSDT"
    assert rows[0]["event_id"]
    assert rows[0]["timestamp"]


def test_journal_deduplicates_after_restart(tmp_path):
    path = tmp_path / "journal.csv"
    event = JournalEvent(
        event_type="order",
        status="FILLED",
        source_event_id="order:1:FILLED:100",
    )
    assert CsvTradeJournal(path).append(event)

    assert not CsvTradeJournal(path).append(event)


def test_pending_take_profit_plan_survives_restart(tmp_path):
    path = tmp_path / "journal.csv"
    plan = TradePlan(
        symbol="BTCUSDT",
        side=OrderSide.SHORT,
        entry_min=50,
        entry_max=50,
        current_price=50,
        in_range=False,
        total_quantity=2,
        stop_loss=55,
        take_profits=(
            PlannedTakeProfit(45, 1),
            PlannedTakeProfit(40, 1),
        ),
        leverage=10,
        risk_percent=1,
        risk_budget=10,
        limit_price=50,
    )
    CsvTradeJournal(path).save_plan("client-1", plan)

    restored = CsvTradeJournal(path).load_pending_plans()

    assert restored["client-1"].symbol == "BTCUSDT"
    assert restored["client-1"].side is OrderSide.SHORT
    assert [item.price for item in restored["client-1"].take_profits] == [
        45,
        40,
    ]

    journal = CsvTradeJournal(path)
    journal.finish_plan("client-1", "CONFIGURED")
    assert journal.load_pending_plans() == {}
