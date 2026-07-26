import csv

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
