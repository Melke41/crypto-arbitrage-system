"""Unit tests for the Binance Spot/Futures Testnet Sandbox Verification Tool."""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from config.settings import SystemSettings
from scripts.verify_testnet import (
    build_signed_params,
    format_verification_summary,
    is_placeholder_key,
    verify_step1_clock_sync,
    verify_step2_spot_account,
    verify_step3_spot_order_lifecycle,
    verify_step4_futures_account_and_leverage,
    verify_step5_kraken_hybrid_check,
)


def test_is_placeholder_key() -> None:
    assert is_placeholder_key("") is True
    assert is_placeholder_key(None) is True
    assert is_placeholder_key("your_binance_key_here") is True
    assert is_placeholder_key("your_binance_futures_key_here") is True
    assert is_placeholder_key("placeholder") is True
    assert is_placeholder_key("ak_live_98372648723648234") is False
    assert is_placeholder_key("real_secret_token_123") is False


def test_build_signed_params_clock_offset_and_recv_window() -> None:
    params = {"symbol": "BTCUSDT", "side": "BUY"}
    secret = "test_secret_123"
    clock_offset = 150  # 150ms ahead

    signed, sig = build_signed_params(
        params, api_secret=secret, clock_offset_ms=clock_offset, recv_window=5000
    )

    assert signed["recvWindow"] == "5000"
    assert "timestamp" in signed
    assert "signature" in signed
    assert signed["signature"] == sig
    assert len(sig) == 64  # SHA256 hex length


def test_settings_dual_binance_keys() -> None:
    settings = SystemSettings(
        binance_api_key="spot_key_1",
        binance_api_secret="spot_sec_1",
        binance_futures_api_key="fut_key_2",
        binance_futures_api_secret="fut_sec_2",
    )
    k_spot, s_spot = settings.get_binance_credentials(is_futures=False)
    k_fut, s_fut = settings.get_binance_credentials(is_futures=True)

    assert k_spot == "spot_key_1"
    assert s_spot == "spot_sec_1"
    assert k_fut == "fut_key_2"
    assert s_fut == "fut_sec_2"

    # Fallback behavior when futures keys are empty
    settings_fallback = SystemSettings(
        binance_api_key="shared_key",
        binance_api_secret="shared_sec",
    )
    kf, sf = settings_fallback.get_binance_credentials(is_futures=True)
    assert kf == "shared_key"
    assert sf == "shared_sec"


class MockResponse:
    def __init__(self, data: dict, status: int = 200) -> None:
        self._data = data
        self.status = status

    async def json(self) -> dict:
        return self._data

    async def __aenter__(self) -> "MockResponse":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        pass


@pytest.mark.asyncio
async def test_verify_step1_clock_sync_mocked() -> None:
    mock_session = MagicMock()
    server_time = 1700000000000

    def mock_get(url: str, **kwargs):
        if "api/v3/time" in url:
            return MockResponse({"serverTime": server_time})
        elif "fapi/v1/time" in url:
            return MockResponse({"serverTime": server_time + 50})
        return MockResponse({}, status=404)

    mock_session.get.side_effect = mock_get

    res = await verify_step1_clock_sync(mock_session)
    assert res["status"] == "PASSED"
    assert "Spot offset:" in res["details"]
    assert "Futures offset:" in res["details"]


@pytest.mark.asyncio
async def test_verify_step2_spot_account_mocked() -> None:
    mock_session = MagicMock()

    account_data = {
        "balances": [
            {"asset": "USDT", "free": "1000.00", "locked": "0.00"},
            {"asset": "BTC", "free": "0.50", "locked": "0.00"},
            {"asset": "ETH", "free": "0.00", "locked": "0.00"},
        ]
    }

    mock_session.get.return_value = MockResponse(account_data, status=200)

    res = await verify_step2_spot_account(
        session=mock_session,
        spot_url="https://testnet.binance.vision",
        api_key="valid_key",
        api_secret="valid_secret",
        clock_offset_ms=0,
    )

    assert res["status"] == "PASSED"
    assert "USDT" in res["balances"]
    assert res["balances"]["USDT"]["free"] == "1000.00"
    assert "BTC" in res["balances"]


@pytest.mark.asyncio
async def test_verify_step3_spot_order_lifecycle_mocked() -> None:
    mock_session = MagicMock()

    mock_session.post.return_value = MockResponse({"orderId": 987654321, "status": "NEW"}, status=200)
    mock_session.delete.return_value = MockResponse({"orderId": 987654321, "status": "CANCELED"}, status=200)

    res = await verify_step3_spot_order_lifecycle(
        session=mock_session,
        spot_url="https://testnet.binance.vision",
        api_key="valid_key",
        api_secret="valid_secret",
    )

    assert res["status"] == "PASSED"
    assert res["order_id"] == "987654321"
    assert "cancelled successfully" in res["details"]


@pytest.mark.asyncio
async def test_verify_step4_futures_account_and_leverage_mocked() -> None:
    mock_session = MagicMock()

    balance_data = [
        {"asset": "USDT", "balance": "5000.00"},
        {"asset": "BNB", "balance": "10.00"},
    ]

    mock_session.get.return_value = MockResponse(balance_data, status=200)
    mock_session.post.return_value = MockResponse({"symbol": "BTCUSDT", "leverage": 2}, status=200)

    res = await verify_step4_futures_account_and_leverage(
        session=mock_session,
        futures_url="https://testnet.binancefuture.com",
        api_key="valid_fut_key",
        api_secret="valid_fut_secret",
    )

    assert res["status"] == "PASSED"
    assert "USDT" in res["futures_balances"]
    assert "Leverage on BTCUSDT set to 2x" in res["details"]


@pytest.mark.asyncio
async def test_verify_step5_kraken_hybrid_check_mocked() -> None:
    mock_session = MagicMock()

    depth_data = {
        "result": {
            "XXBTZUSD": {
                "bids": [["50000.0", "1.0", 1700000000]],
                "asks": [["50010.0", "1.0", 1700000000]],
            }
        }
    }

    mock_session.get.return_value = MockResponse(depth_data, status=200)
    settings = SystemSettings()

    res = await verify_step5_kraken_hybrid_check(mock_session, settings)
    assert res["status"] == "PASSED"
    assert "Kraken live depth healthy" in res["details"]
    assert "Hybrid TestnetExecutor initialized" in res["details"]


def test_format_verification_summary_with_missing_keys() -> None:
    settings = SystemSettings(
        binance_api_key="your_binance_key_here",
        binance_api_secret="",
    )
    steps = [
        {"name": "Step 1: Clock Sync", "status": "PASSED", "details": "Offset: 5ms"},
        {"name": "Step 2: Spot Account", "status": "SKIPPED", "details": "Missing credentials"},
    ]
    report = format_verification_summary(
        steps=steps,
        settings=settings,
        missing_keys=["BINANCE_API_KEY", "BINANCE_API_SECRET"],
    )

    assert "BINANCE SPOT / FUTURES TESTNET & SANDBOX" in report
    assert "[MISSING/PLACEHOLDER]" in report
    assert "ACTION REQUIRED TO RUN AUTHENTICATED TESTNET TRADING" in report
    assert "https://testnet.binance.vision" in report
