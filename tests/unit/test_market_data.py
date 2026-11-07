"""Unit tests for Phase 1 Market Data Engine.

Verifies:
- Symbol normalization between native and canonical representations.
- Binance Spot L2 sequence tracking, diff buffering, and gap desync detection.
- Kraken Spot v2 CRC32 checksum verification and corruption detection.
- Crossed-book and stale-latency gates.
"""

import time
import zlib
from decimal import Decimal
import pytest

from src.core.exceptions import ChecksumMismatchError, OrderBookDesyncError
from src.core.models import ExchangeId, NormalizedOrderBook, OrderBookLevel
from src.market_data.market_registry import MarketRegistry
from src.market_data.normalizer import SymbolNormalizer, normalize_binance_snapshot, normalize_kraken_snapshot
from src.market_data.orderbook import (
    OrderBookEngineImpl,
    format_kraken_crc_value,
)


# ==============================================================================
# Symbol Normalization & Registry Tests
# ==============================================================================


def test_symbol_normalizer() -> None:
    """Verify bidirectional ticker conversion for Binance and Kraken."""
    # Binance
    assert SymbolNormalizer.to_canonical(ExchangeId.BINANCE, "BTCUSDT") == "BTC/USDT"
    assert SymbolNormalizer.to_canonical(ExchangeId.BINANCE, "ETHBTC") == "ETH/BTC"
    assert SymbolNormalizer.to_canonical(ExchangeId.BINANCE, "SOLFDUSD") == "SOL/FDUSD"
    assert SymbolNormalizer.to_native(ExchangeId.BINANCE, "BTC/USDT") == "BTCUSDT"

    # Kraken
    assert SymbolNormalizer.to_canonical(ExchangeId.KRAKEN, "BTC/USDT") == "BTC/USDT"
    assert SymbolNormalizer.to_canonical(ExchangeId.KRAKEN, "XBT/USDT") == "BTC/USDT"
    assert SymbolNormalizer.to_canonical(ExchangeId.KRAKEN, "BTC/USD") == "BTC/USD"
    assert SymbolNormalizer.to_canonical(ExchangeId.KRAKEN, "XXBTZUSD") == "BTC/USD"
    assert SymbolNormalizer.to_native(ExchangeId.KRAKEN, "BTC/USDT") == "BTC/USDT"


def test_market_registry_filtering() -> None:
    """Verify registry parses and stores SymbolMetadata correctly."""
    registry = MarketRegistry()

    # Mock Binance symbol info
    binance_raw = {
        "symbol": "BTCUSDT",
        "baseAsset": "BTC",
        "quoteAsset": "USDT",
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
            {"filterType": "LOT_SIZE", "stepSize": "0.00001", "minQty": "0.0001", "maxQty": "100.0"},
            {"filterType": "NOTIONAL", "minNotional": "5.0"},
        ],
    }
    meta_b = MarketRegistry.parse_binance_symbol_info(binance_raw)
    registry.register(meta_b)

    retrieved_b = registry.get(ExchangeId.BINANCE, "BTC/USDT")
    assert retrieved_b is not None
    assert retrieved_b.tick_size == Decimal("0.01")
    assert retrieved_b.lot_size == Decimal("0.00001")
    assert retrieved_b.min_notional == Decimal("5.0")

    # Mock Kraken asset pair
    kraken_raw = {
        "base": "XXBT",
        "quote": "ZUSD",
        "wsname": "BTC/USD",
        "pair_decimals": 1,
        "lot_decimals": 8,
        "ordermin": "0.0001",
        "costmin": "5.0",
    }
    meta_k = MarketRegistry.parse_kraken_asset_pair("XXBTZUSD", kraken_raw)
    registry.register(meta_k)

    retrieved_k = registry.get(ExchangeId.KRAKEN, "BTC/USD")
    assert retrieved_k is not None
    assert retrieved_k.tick_size == Decimal("0.1")
    assert retrieved_k.min_qty == Decimal("0.0001")


# ==============================================================================
# Binance L2 Sequence Validation Tests
# ==============================================================================


def test_binance_l2_sync_and_gap_detection() -> None:
    """Verify Binance sequence tracking: dropped stale diffs, valid progression, and gap desync."""
    engine = OrderBookEngineImpl(max_data_age_ms=500)
    now_ms = int(time.time() * 1000)

    # 1. Apply REST snapshot with lastUpdateId = 100
    snapshot = NormalizedOrderBook(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT",
        bids=[OrderBookLevel(price=Decimal("50000.00"), quantity=Decimal("1.0"))],
        asks=[OrderBookLevel(price=Decimal("50010.00"), quantity=Decimal("1.0"))],
        sequence_id=100,
        exchange_timestamp_ms=now_ms,
        local_receipt_timestamp_ms=now_ms,
    )
    engine.apply_snapshot(snapshot)

    book = engine.get_book(ExchangeId.BINANCE, "BTC/USDT")
    assert book is not None
    assert not book.is_stale
    assert book.sequence_id == 100

    # 2. Drop older or stale diff where u <= lastUpdateId (u=95)
    stale_diff = {"e": "depthUpdate", "s": "BTCUSDT", "U": 90, "u": 95, "b": [], "a": []}
    book = engine.apply_binance_diff(stale_diff, receipt_timestamp_ms=now_ms)
    assert book.sequence_id == 100  # Not mutated

    # 3. Valid first diff where U <= lastUpdateId + 1 and u >= lastUpdateId + 1 (U=100, u=105)
    valid_diff_1 = {
        "e": "depthUpdate",
        "s": "BTCUSDT",
        "U": 100,
        "u": 105,
        "b": [["50005.00", "2.0"]],
        "a": [["50015.00", "0.5"]],
    }
    book = engine.apply_binance_diff(valid_diff_1, receipt_timestamp_ms=now_ms)
    assert not book.is_stale
    assert book.sequence_id == 105
    assert book.best_bid.price == Decimal("50005.00")
    assert book.best_bid.quantity == Decimal("2.0")

    # 4. Valid subsequent diff where U == prev_u + 1 (U=106, u=110)
    valid_diff_2 = {
        "e": "depthUpdate",
        "s": "BTCUSDT",
        "U": 106,
        "u": 110,
        "b": [["50005.00", "0.0"]],  # Remove level
        "a": [],
    }
    book = engine.apply_binance_diff(valid_diff_2, receipt_timestamp_ms=now_ms)
    assert not book.is_stale
    assert book.sequence_id == 110
    # Level 50005.00 was deleted, best bid should revert to 50000.00
    assert book.best_bid.price == Decimal("50000.00")

    # 5. Sequence gap: expected U=111, but received U=115 -> raises OrderBookDesyncError
    gap_diff = {
        "e": "depthUpdate",
        "s": "BTCUSDT",
        "U": 115,
        "u": 120,
        "b": [["50002.00", "1.0"]],
        "a": [],
    }
    with pytest.raises(OrderBookDesyncError) as exc_info:
        engine.apply_binance_diff(gap_diff, receipt_timestamp_ms=now_ms)
    assert "Binance sequence gap" in str(exc_info.value)

    # Book is now marked stale
    assert engine.is_stale(ExchangeId.BINANCE, "BTC/USDT")


# ==============================================================================
# Kraken v2 CRC32 Checksum Tests
# ==============================================================================


def test_kraken_crc32_formatting() -> None:
    """Verify Kraken v2 value formatting: strip decimal points and leading zeros."""
    assert format_kraken_crc_value(Decimal("50000.00")) == "5000000"
    assert format_kraken_crc_value(Decimal("0.00123")) == "123"
    assert format_kraken_crc_value(Decimal("0.0")) == "0"
    assert format_kraken_crc_value(Decimal("65432.1")) == "654321"


def test_kraken_v2_checksum_verification_and_mismatch() -> None:
    """Verify Kraken v2 book snapshot, incremental update, and CRC32 verification."""
    engine = OrderBookEngineImpl()
    now_ms = int(time.time() * 1000)

    # Compute expected CRC32 for deterministic snapshot
    # Ask: 50010.0 (qty 1.0) -> "500100" + "10"
    # Bid: 50000.0 (qty 1.5) -> "500000" + "15"
    expected_str = "5001001050000015"
    expected_crc = zlib.crc32(expected_str.encode("utf-8")) & 0xFFFFFFFF

    snapshot_msg = {
        "channel": "book",
        "type": "snapshot",
        "data": [
            {
                "symbol": "BTC/USDT",
                "checksum": expected_crc,
                "bids": [{"price": 50000.0, "qty": 1.5}],
                "asks": [{"price": 50010.0, "qty": 1.0}],
            }
        ],
    }

    snapshot = normalize_kraken_snapshot(snapshot_msg, receipt_timestamp_ms=now_ms)
    assert snapshot is not None
    engine.apply_snapshot(snapshot)

    book = engine.get_book(ExchangeId.KRAKEN, "BTC/USDT")
    assert book is not None
    assert not book.is_stale
    assert engine.verify_checksum(ExchangeId.KRAKEN, "BTC/USDT", expected_crc)

    # Now apply incremental update with valid matching checksum
    # New ask at 50020.0 (qty 2.0)
    # Book has:
    # Asks: 50010.0 (1.0), 50020.0 (2.0)
    # Bids: 50000.0 (1.5)
    new_expected_str = "500100105002002050000015"
    new_crc = zlib.crc32(new_expected_str.encode("utf-8")) & 0xFFFFFFFF

    update_msg = {
        "channel": "book",
        "type": "update",
        "data": [
            {
                "symbol": "BTC/USDT",
                "checksum": new_crc,
                "bids": [],
                "asks": [{"price": 50020.0, "qty": 2.0}],
            }
        ],
    }
    book = engine.apply_kraken_update(update_msg, receipt_timestamp_ms=now_ms)
    assert not book.is_stale
    assert len(book.asks) == 2

    # Now simulate corrupted update with checksum mismatch
    corrupted_update = {
        "channel": "book",
        "type": "update",
        "data": [
            {
                "symbol": "BTC/USDT",
                "checksum": 12345678,  # Intentional invalid checksum
                "bids": [{"price": 50005.00, "qty": 0.5}],
                "asks": [],
            }
        ],
    }
    with pytest.raises(ChecksumMismatchError) as exc_info:
        engine.apply_kraken_update(corrupted_update, receipt_timestamp_ms=now_ms)
    assert "checksum mismatch" in str(exc_info.value)
    assert engine.is_stale(ExchangeId.KRAKEN, "BTC/USDT")


# ==============================================================================
# Crossed Book & Stale Data Latency Gate Tests
# ==============================================================================


def test_crossed_book_and_staleness_rejection() -> None:
    """Verify that crossed books and expired timestamps are flagged stale."""
    engine = OrderBookEngineImpl(max_data_age_ms=250)
    old_time = int(time.time() * 1000) - 300  # 300ms old

    # Crossed book snapshot: best bid (50020.00) >= best ask (50010.00)
    crossed_snapshot = NormalizedOrderBook(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT",
        bids=[OrderBookLevel(price=Decimal("50020.00"), quantity=Decimal("1.0"))],
        asks=[OrderBookLevel(price=Decimal("50010.00"), quantity=Decimal("1.0"))],
        sequence_id=50,
        exchange_timestamp_ms=old_time,
        local_receipt_timestamp_ms=old_time,
    )
    engine.apply_snapshot(crossed_snapshot)

    book = engine.get_book(ExchangeId.BINANCE, "BTC/USDT")
    assert book is not None
    # Marked stale because of crossed book and latency > 250ms
    assert book.is_stale
    assert engine.is_stale(ExchangeId.BINANCE, "BTC/USDT")
