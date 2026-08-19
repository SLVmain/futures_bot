import pytest

from models.signal import OrderSide
from services.signal_parser import SignalParser


@pytest.mark.parametrize(
    ("side_text", "expected_side"),
    [
        ("ПОКУПКА", OrderSide.LONG),
        ("ПРОДАЖА", OrderSide.SHORT),
    ],
)
def test_parses_russian_signal(side_text, expected_side):
    message = f"""#BTC/USDT.P {side_text}
Диапазон входа: 60,5-61,5
Тейк-профит 1: 62.5
Тейк-профит 2: 63.5
Стоп-лосс: 59.5
"""

    signal = SignalParser.parse(message)

    assert signal is not None
    assert signal.symbol == "BTCUSDT"
    assert signal.side is expected_side
    assert signal.entry_min == 60.5
    assert signal.entry_max == 61.5
    assert signal.take_profits == [62.5, 63.5]
    assert signal.stop_loss == 59.5


def test_returns_none_for_message_without_direction():
    message = """#BTCUSDT
Диапазон входа: 60-61
Тейк-профит 1: 62
Стоп-лосс: 59
"""

    assert SignalParser.parse(message) is None


@pytest.mark.parametrize(
    ("side_text", "stop_range", "expected_stop"),
    [
        ("LONG", "59,5-58,5", 58.5),
        ("SHORT", "62,5-63,5", 63.5),
    ],
)
def test_stop_range_uses_farthest_loss_boundary(
    side_text,
    stop_range,
    expected_stop,
):
    message = f"""#BTCUSDT {side_text}
Диапазон входа: 60-61
Цели: 62 / 63 / 64
Диапазон стопа: {stop_range}
"""

    signal = SignalParser.parse(message)

    assert signal is not None
    assert signal.stop_loss == expected_stop
