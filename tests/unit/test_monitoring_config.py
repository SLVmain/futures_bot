from pathlib import Path

import pytest

from config.execution import ExecutionMode
from config.monitoring import MonitoringConfig


def test_dry_run_always_disables_private_websocket():
    config = MonitoringConfig.from_env(
        {"ENABLE_PRIVATE_WEBSOCKET": "true"},
        ExecutionMode.DRY_RUN,
    )

    assert config.enabled is False
    assert config.websocket_url is None
    assert config.auto_move_stop_loss_on_tp1 is True


def test_live_monitoring_uses_official_private_url():
    config = MonitoringConfig.from_env(
        {"ENABLE_PRIVATE_WEBSOCKET": "true"},
        ExecutionMode.LIVE,
    )

    assert config.enabled is True
    assert config.websocket_url == "wss://fapi.bitunix.com/private/"
    assert config.journal_path == Path("data/trade_journal.csv")
    assert config.auto_move_stop_loss_on_tp1 is True


def test_auto_break_even_can_be_disabled():
    config = MonitoringConfig.from_env(
        {
            "ENABLE_PRIVATE_WEBSOCKET": "true",
            "AUTO_MOVE_STOP_LOSS_ON_TP1": "false",
        },
        ExecutionMode.LIVE,
    )

    assert config.auto_move_stop_loss_on_tp1 is False


def test_auto_break_even_rejects_unknown_value():
    with pytest.raises(ValueError, match="AUTO_MOVE_STOP_LOSS_ON_TP1"):
        MonitoringConfig.from_env(
            {"AUTO_MOVE_STOP_LOSS_ON_TP1": "yes"},
            ExecutionMode.DRY_RUN,
        )


def test_live_mode_requires_private_monitoring():
    with pytest.raises(ValueError, match="required"):
        MonitoringConfig.from_env({}, ExecutionMode.LIVE)


def test_testnet_requires_separate_websocket_url():
    with pytest.raises(ValueError, match="required"):
        MonitoringConfig.from_env(
            {"ENABLE_PRIVATE_WEBSOCKET": "true"},
            ExecutionMode.TESTNET,
        )
