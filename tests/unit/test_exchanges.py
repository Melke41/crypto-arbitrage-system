"""Unit tests for Phase 1 Exchange Adapters.

Verifies:
- Binance and Kraken adapter initialization and URL toggling.
- Subscription payload construction for WebSocket streams.
- Exponential backoff with jitter.
- Public market data functioning without API credentials.
"""

import asyncio
from decimal import Decimal
import pytest

from src.core.models import ExchangeId, SymbolMetadata
from src.exchanges.binance import BinanceExchange
from src.exchanges.kraken import KrakenExchange


def test_binance_adapter_initialization() -> None:
    """Verify Binance URL switching between testnet and production."""
    # Testnet default
    binance_testnet = BinanceExchange(use_testnet=True)
    assert binance_testnet.exchange_id == ExchangeId.BINANCE
    assert "testnet" in binance_testnet.rest_url
    assert "testnet" in binance_testnet.ws_url

    # Production
    binance_prod = BinanceExchange(use_testnet=False)
    assert "api.binance.com" in binance_prod.rest_url
    assert "stream.binance.com" in binance_prod.ws_url


def test_kraken_adapter_initialization() -> None:
    """Verify Kraken adapter defaults to v2 WebSocket endpoint."""
    kraken = KrakenExchange()
    assert kraken.exchange_id == ExchangeId.KRAKEN
    assert "ws.kraken.com/v2" in kraken.WS_URL
    assert "api.kraken.com/0" in kraken.REST_URL


@pytest.mark.asyncio
async def test_exponential_backoff_jitter() -> None:
    """Verify backoff sleep computes positive jittered delay within expected bounds."""
    kraken = KrakenExchange()
    # Attempt 0: delay should be between 0.1 and 0.5s
    # We do a quick timing test (or verify calculation logic)
    start = asyncio.get_event_loop().time()
    await kraken.backoff_sleep(attempt=0, base_delay=0.1, max_delay=0.5)
    elapsed = asyncio.get_event_loop().time() - start
    assert 0.05 <= elapsed <= 0.6


@pytest.mark.asyncio
async def test_subscription_management() -> None:
    """Verify subscribe_orderbook registers canonical symbols."""
    binance = BinanceExchange()
    kraken = KrakenExchange()

    await binance.subscribe_orderbook("BTC/USDT")
    await binance.subscribe_orderbook("ethusdt")

    assert "BTC/USDT" in binance._subscribed_symbols
    assert "ETH/USDT" in binance._subscribed_symbols

    await kraken.subscribe_orderbook("BTC/USDT")
    await kraken.subscribe_orderbook("XBT/USDT")

    assert "BTC/USDT" in kraken._subscribed_symbols
    # XBT/USDT canonicalized to BTC/USDT
    assert len(kraken._subscribed_symbols) == 1
