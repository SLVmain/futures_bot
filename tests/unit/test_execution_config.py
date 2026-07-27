import pytest

from config.execution import (
    PRODUCTION_BASE_URL,
    ExecutionConfig,
    ExecutionMode,
)


def test_defaults_to_dry_run():
    config = ExecutionConfig.from_env({})

    assert config.mode is ExecutionMode.DRY_RUN
    assert config.base_url == PRODUCTION_BASE_URL
    assert config.is_dry_run is True


def test_rejects_unknown_mode():
    with pytest.raises(ValueError, match="Unknown TRADING_MODE"):
        ExecutionConfig.from_env({"TRADING_MODE": "paper"})


def test_live_mode_uses_single_mode_variable():
    config = ExecutionConfig.from_env({"TRADING_MODE": "live"})

    assert config.mode is ExecutionMode.LIVE
    assert config.base_url == PRODUCTION_BASE_URL
    assert config.is_dry_run is False


def test_testnet_requires_explicit_url():
    with pytest.raises(ValueError, match="BITUNIX_TESTNET_BASE_URL"):
        ExecutionConfig.from_env({"TRADING_MODE": "testnet"})


@pytest.mark.parametrize(
    "url",
    [
        PRODUCTION_BASE_URL,
        f"{PRODUCTION_BASE_URL}/",
        "http://testnet.example.com",
    ],
)
def test_testnet_rejects_unsafe_url(url):
    with pytest.raises(ValueError):
        ExecutionConfig.from_env(
            {
                "TRADING_MODE": "testnet",
                "BITUNIX_TESTNET_BASE_URL": url,
            }
        )


def test_testnet_accepts_custom_https_url():
    config = ExecutionConfig.from_env(
        {
            "TRADING_MODE": "testnet",
            "BITUNIX_TESTNET_BASE_URL": "https://testnet.example.com/",
        }
    )

    assert config.mode is ExecutionMode.TESTNET
    assert config.base_url == "https://testnet.example.com"
