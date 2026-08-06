import pytest

from config.settings import TradeSettings


def test_take_profit_offset_defaults_to_two_ticks():
    assert TradeSettings.take_profit_offset_from_env({}) == 2


def test_take_profit_offset_can_be_disabled():
    assert TradeSettings.take_profit_offset_from_env({
        "TAKE_PROFIT_OFFSET_TICKS": "0",
    }) == 0


def test_take_profit_offset_rejects_negative_value():
    with pytest.raises(ValueError, match="zero or positive"):
        TradeSettings.take_profit_offset_from_env({
            "TAKE_PROFIT_OFFSET_TICKS": "-1",
        })


def test_take_profit_offset_rejects_non_integer():
    with pytest.raises(ValueError, match="integer"):
        TradeSettings.take_profit_offset_from_env({
            "TAKE_PROFIT_OFFSET_TICKS": "1.5",
        })
