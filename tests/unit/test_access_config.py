import pytest

from config.access import TelegramAccessConfig
from config.execution import ExecutionMode


def test_empty_allowlist_is_allowed_outside_live_mode():
    config = TelegramAccessConfig.from_env({}, ExecutionMode.DRY_RUN)

    assert config.is_allowed(123)


def test_allowlist_restricts_users():
    config = TelegramAccessConfig.from_env(
        {"TELEGRAM_ALLOWED_USER_IDS": "123, 456"},
        ExecutionMode.LIVE,
    )

    assert config.is_allowed(123)
    assert not config.is_allowed(789)


def test_live_mode_requires_allowlist():
    with pytest.raises(ValueError, match="required"):
        TelegramAccessConfig.from_env({}, ExecutionMode.LIVE)


def test_allowlist_rejects_non_integer_values():
    with pytest.raises(ValueError, match="integers"):
        TelegramAccessConfig.from_env(
            {"TELEGRAM_ALLOWED_USER_IDS": "123,user"},
            ExecutionMode.DRY_RUN,
        )
