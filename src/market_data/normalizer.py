"""Market data and symbol normalizer for Binance Spot and Kraken Spot.

Normalizes tickers, raw WebSocket depth events, and REST snapshots into
canonical system symbols and NormalizedOrderBook domain models.
"""

from __future__ import annotations

import re
import time
from decimal import Decimal
from typing import Any, Optional

from src.core.models import (
    ExchangeId,
    NormalizedOrderBook,
    OrderBookLevel,
    SymbolMetadata,
)


class SymbolNormalizer:
    """Two-way mapper between exchange-native ticker strings and canonical BASE/QUOTE symbols."""

    # Common base assets that need special mapping on Kraken legacy/modern
    KRAKEN_ASSET_MAP = {
        "XBT": "BTC",
        "XXBT": "BTC",
        "XETH": "ETH",
        "ZUSD": "USD",
        "ZEUR": "EUR",
        "USDT": "USDT",
    }

    REVERSE_KRAKEN_MAP = {
        "BTC": "BTC",
        "ETH": "ETH",
        "USD": "USD",
        "EUR": "EUR",
        "USDT": "USDT",
    }

    @classmethod
    def to_canonical(cls, exchange: ExchangeId, native_symbol: str) -> str:
        """Convert exchange ticker to canonical standard 'BASE/QUOTE' (e.g. BTC/USDT)."""
        clean = native_symbol.strip().upper()

        # If slash is already present, sanitize both components
        if "/" in clean:
            parts = clean.split("/")
            if len(parts) == 2:
                base = cls.KRAKEN_ASSET_MAP.get(parts[0], parts[0])
                quote = cls.KRAKEN_ASSET_MAP.get(parts[1], parts[1])
                return f"{base}/{quote}"
            return clean

        if exchange == ExchangeId.BINANCE:
            # Handles pairs like BTCUSDT, ETHBTC, SOLUSDT
            quotes = ["USDT", "FDUSD", "TUSD", "BUSD", "USDC", "BTC", "ETH", "BNB", "EUR"]
            for q in quotes:
                if clean.endswith(q) and len(clean) > len(q):
                    base = clean[: -len(q)]
                    return f"{base}/{q}"
            return clean

        elif exchange == ExchangeId.KRAKEN:
            # Kraken v2 often provides 'BTC/USD', 'BTC/USDT', or 'XBT/USDT'
            if "/" in clean:
                parts = clean.split("/")
                base = cls.KRAKEN_ASSET_MAP.get(parts[0], parts[0])
                quote = cls.KRAKEN_ASSET_MAP.get(parts[1], parts[1])
                return f"{base}/{quote}"
            # Legacy REST pairs like XXBTZUSD
            if clean.startswith("X") and len(clean) == 8 and clean[4] == "Z":
                base = cls.KRAKEN_ASSET_MAP.get(clean[:4], clean[1:4])
                quote = cls.KRAKEN_ASSET_MAP.get(clean[4:], clean[5:])
                return f"{base}/{quote}"
            return clean

        return clean

    @classmethod
    def to_native(cls, exchange: ExchangeId, canonical_symbol: str) -> str:
        """Convert canonical symbol 'BASE/QUOTE' to exchange native ticker."""
        parts = canonical_symbol.strip().upper().split("/")
        if len(parts) != 2:
            return canonical_symbol

        base, quote = parts[0], parts[1]

        if exchange == ExchangeId.BINANCE:
            return f"{base}{quote}"
        elif exchange == ExchangeId.KRAKEN:
            # Kraken WebSocket v2 uses 'BTC/USDT' or 'BTC/USD'
            return f"{base}/{quote}"

        return canonical_symbol


def parse_levels(levels_raw: list[list[Any]]) -> list[OrderBookLevel]:
    """Parse raw [price, quantity] pairs into OrderBookLevel instances."""
    parsed: list[OrderBookLevel] = []
    for item in levels_raw:
        if len(item) >= 2:
            price = Decimal(str(item[0]))
            qty = Decimal(str(item[1]))
            parsed.append(OrderBookLevel(price=price, quantity=qty))
    return parsed


def normalize_binance_snapshot(
    symbol: str, raw_snapshot: dict[str, Any], receipt_timestamp_ms: Optional[int] = None
) -> NormalizedOrderBook:
    """Normalize Binance REST /api/v3/depth snapshot into NormalizedOrderBook."""
    canonical = SymbolNormalizer.to_canonical(ExchangeId.BINANCE, symbol)
    last_update_id = int(raw_snapshot.get("lastUpdateId", 0))
    now_ms = receipt_timestamp_ms or int(time.time() * 1000)

    bids = parse_levels(raw_snapshot.get("bids", []))
    asks = parse_levels(raw_snapshot.get("asks", []))

    # Sort bids descending, asks ascending
    bids.sort(key=lambda x: x.price, reverse=True)
    asks.sort(key=lambda x: x.price, reverse=False)

    return NormalizedOrderBook(
        exchange=ExchangeId.BINANCE,
        symbol=canonical,
        bids=bids,
        asks=asks,
        sequence_id=last_update_id,
        exchange_timestamp_ms=now_ms,
        local_receipt_timestamp_ms=now_ms,
        checksum=None,
        is_stale=False,
    )


def normalize_kraken_snapshot(
    raw_message: dict[str, Any], receipt_timestamp_ms: Optional[int] = None
) -> Optional[NormalizedOrderBook]:
    """Normalize Kraken v2 WebSocket 'book' snapshot message into NormalizedOrderBook."""
    data_list = raw_message.get("data", [])
    if not data_list:
        return None

    data = data_list[0]
    raw_symbol = data.get("symbol", "")
    canonical = SymbolNormalizer.to_canonical(ExchangeId.KRAKEN, raw_symbol)
    checksum = data.get("checksum")
    now_ms = receipt_timestamp_ms or int(time.time() * 1000)

    # Kraken v2 snapshot levels: [{'price': 60000.0, 'qty': 1.5}, ...]
    bids_raw = data.get("bids", [])
    asks_raw = data.get("asks", [])

    bids: list[OrderBookLevel] = []
    for item in bids_raw:
        if isinstance(item, dict):
            bids.append(OrderBookLevel(price=Decimal(str(item["price"])), quantity=Decimal(str(item["qty"]))))
        elif isinstance(item, (list, tuple)):
            bids.append(OrderBookLevel(price=Decimal(str(item[0])), quantity=Decimal(str(item[1]))))

    asks: list[OrderBookLevel] = []
    for item in asks_raw:
        if isinstance(item, dict):
            asks.append(OrderBookLevel(price=Decimal(str(item["price"])), quantity=Decimal(str(item["qty"]))))
        elif isinstance(item, (list, tuple)):
            asks.append(OrderBookLevel(price=Decimal(str(item[0])), quantity=Decimal(str(item[1]))))

    bids.sort(key=lambda x: x.price, reverse=True)
    asks.sort(key=lambda x: x.price, reverse=False)

    return NormalizedOrderBook(
        exchange=ExchangeId.KRAKEN,
        symbol=canonical,
        bids=bids,
        asks=asks,
        sequence_id=0,
        exchange_timestamp_ms=now_ms,
        local_receipt_timestamp_ms=now_ms,
        checksum=int(checksum) if checksum is not None else None,
        is_stale=False,
    )
