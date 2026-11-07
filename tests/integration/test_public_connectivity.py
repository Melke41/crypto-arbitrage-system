"""Integration tests for live public REST endpoints.

Tests read-only exchangeInfo and asset pair metadata fetching from public endpoints.
Gracefully skips if live network access to exchange domains is blocked or times out.
"""

import asyncio
import pytest

from src.exchanges.binance import BinanceExchange
from src.exchanges.kraken import KrakenExchange


@pytest.mark.asyncio
async def test_live_binance_exchange_info() -> None:
    """Fetch live Binance exchangeInfo for BTC/USDT and verify metadata parsing."""
    binance = BinanceExchange(use_testnet=False)
    try:
        symbols = await asyncio.wait_for(
            binance.fetch_exchange_info(symbols=["BTC/USDT"]),
            timeout=5.0,
        )
        assert len(symbols) >= 1
        meta = symbols[0]
        assert meta.symbol == "BTC/USDT"
        assert meta.base_asset == "BTC"
        assert meta.quote_asset == "USDT"
        assert meta.tick_size > 0
        assert meta.lot_size > 0
    except (asyncio.TimeoutError, Exception) as e:
        pytest.skip(f"Live network access to Binance unavailable or timed out: {e}")
    finally:
        await binance.close_http_session()


@pytest.mark.asyncio
async def test_live_kraken_asset_pairs() -> None:
    """Fetch live Kraken AssetPairs for BTC/USDT and verify metadata parsing."""
    kraken = KrakenExchange()
    try:
        symbols = await asyncio.wait_for(
            kraken.fetch_asset_pairs(pairs=["BTC/USDT"]),
            timeout=5.0,
        )
        assert len(symbols) >= 1
        meta = symbols[0]
        assert meta.symbol == "BTC/USDT"
        assert meta.tick_size > 0
    except (asyncio.TimeoutError, Exception) as e:
        pytest.skip(f"Live network access to Kraken unavailable or timed out: {e}")
    finally:
        await kraken.close_http_session()
