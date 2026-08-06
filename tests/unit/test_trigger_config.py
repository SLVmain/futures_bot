from pathlib import Path
from decimal import Decimal

import pytest

from config.triggers import TriggerConfig


def test_trigger_config_defaults_are_safe_and_persistent():
    config = TriggerConfig.from_env({})

    assert config.enabled is True
    assert config.state_path == Path("data/emulated_triggers.json")
    assert config.poll_interval == 3
    assert config.max_age_seconds == 86400
    assert config.limit_offset_ticks == 2
    assert config.price_retry_count == 3
    assert config.price_retry_delay == 3
    assert config.max_entry_deviation_percent == Decimal("0.15")
    assert config.limit_timeout_seconds == 30


def test_trigger_config_can_be_disabled():
    assert TriggerConfig.from_env({
        "ENABLE_EMULATED_ENTRY_TRIGGERS": "false"
    }).enabled is False


def test_trigger_poll_interval_rejects_busy_loop():
    with pytest.raises(ValueError, match="at least 1"):
        TriggerConfig.from_env({"TRIGGER_POLL_SECONDS": "0.1"})


def test_trigger_limit_offset_can_be_configured():
    config = TriggerConfig.from_env({
        "TRIGGER_LIMIT_OFFSET_TICKS": "1",
    })

    assert config.limit_offset_ticks == 1


def test_trigger_limit_offset_must_be_positive():
    with pytest.raises(ValueError, match="at least 1"):
        TriggerConfig.from_env({"TRIGGER_LIMIT_OFFSET_TICKS": "0"})


def test_trigger_safety_settings_can_be_configured():
    config = TriggerConfig.from_env({
        "TRIGGER_PRICE_RETRY_COUNT": "4",
        "TRIGGER_PRICE_RETRY_DELAY_SECONDS": "1.5",
        "TRIGGER_MAX_ENTRY_DEVIATION_PERCENT": "0.2",
        "TRIGGER_LIMIT_TIMEOUT_SECONDS": "45",
    })

    assert config.price_retry_count == 4
    assert config.price_retry_delay == 1.5
    assert config.max_entry_deviation_percent == Decimal("0.2")
    assert config.limit_timeout_seconds == 45


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("TRIGGER_PRICE_RETRY_COUNT", "0"),
        ("TRIGGER_PRICE_RETRY_DELAY_SECONDS", "-1"),
        ("TRIGGER_MAX_ENTRY_DEVIATION_PERCENT", "0"),
        ("TRIGGER_LIMIT_TIMEOUT_SECONDS", "0"),
    ],
)
def test_trigger_safety_settings_reject_unsafe_values(name, value):
    with pytest.raises(ValueError):
        TriggerConfig.from_env({name: value})
