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


def test_live_monitoring_uses_official_private_url():
    config = MonitoringConfig.from_env(
        {"ENABLE_PRIVATE_WEBSOCKET": "true"},
        ExecutionMode.LIVE,
    )

    assert config.enabled is True
    assert config.websocket_url == "wss://fapi.bitunix.com/private/"
    assert config.journal_path == Path("data/trade_journal.csv")


def test_testnet_requires_separate_websocket_url():
    with pytest.raises(ValueError, match="required"):
        MonitoringConfig.from_env(
            {"ENABLE_PRIVATE_WEBSOCKET": "true"},
            ExecutionMode.TESTNET,
        )
