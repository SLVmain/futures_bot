from pathlib import Path

import pytest

from config.triggers import TriggerConfig


def test_trigger_config_defaults_are_safe_and_persistent():
    config = TriggerConfig.from_env({})

    assert config.enabled is True
    assert config.state_path == Path("data/emulated_triggers.json")
    assert config.poll_interval == 3
    assert config.max_age_seconds == 86400
    assert config.limit_offset_ticks == 2


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
