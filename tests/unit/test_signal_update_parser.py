import pytest

from models.signal_update import SignalUpdateType
from services.signal_update_parser import SignalUpdateParser


@pytest.mark.parametrize(
    ("message", "event_type", "symbol"),
    [
        (
            "#GRASS идея закрыта в безубытке",
            SignalUpdateType.CLOSE_BREAK_EVEN,
            "GRASSUSDT",
        ),
        (
            "#XRP отмена, цена ушла",
            SignalUpdateType.CANCEL_ENTRY,
            "XRPUSDT",
        ),
        (
            "#ONDO идея закрывается по стопу (-1%)",
            SignalUpdateType.STOP_REPORTED,
            "ONDOUSDT",
        ),
        (
            "#BNB отмена",
            SignalUpdateType.CANCEL_ENTRY,
            "BNBUSDT",
        ),
        (
            "#LTC была достигнута первая цель (+1,13%)",
            SignalUpdateType.TP1_REPORTED,
            "LTCUSDT",
        ),
        (
            "#XPL закрываю идею по текущим ценам (-0,25%)",
            SignalUpdateType.CLOSE_MARKET,
            "XPLUSDT",
        ),
    ],
)
def test_parses_signal_update(message, event_type, symbol):
    result = SignalUpdateParser.parse(message)

    assert result is not None
    assert result.event_type is event_type
    assert result.symbol == symbol


def test_parses_opposite_trade_message_after_photo():
    result = SignalUpdateParser.parse(
        "❗️#PUMPUSDT.P Закрыто из-за противоположной сделки!\n\n"
        "Цена закрытия: 0.002217\n\n"
        "Прибыль: -7.15%"
    )

    assert result is not None
    assert result.event_type is SignalUpdateType.CLOSE_OPPOSITE
    assert result.symbol == "PUMPUSDT"
    assert result.reported_price == "0.002217"
    assert result.reported_percent == "-7.15"


def test_ordinary_entry_signal_is_not_an_update():
    assert SignalUpdateParser.parse(
        "#BTCUSDT LONG\nДиапазон входа: 50-51"
    ) is None


def test_update_requires_symbol():
    assert SignalUpdateParser.parse("идея закрыта в безубытке") is None
