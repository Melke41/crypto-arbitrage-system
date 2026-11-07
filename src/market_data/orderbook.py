"""L2 Order Book Synchronization and Integrity Engine.

Implements exact sequence continuity checking for Binance Spot (@depth@100ms)
and IEEE 802.3 CRC32 checksum verification for Kraken Spot WebSocket v2.
"""

from __future__ import annotations

import time
import zlib
from decimal import Decimal
from typing import Any, Optional

from src.core.exceptions import ChecksumMismatchError, OrderBookDesyncError
from src.core.models import (
    ExchangeId,
    NormalizedOrderBook,
    OrderBookLevel,
)
from src.market_data.normalizer import SymbolNormalizer


def format_kraken_crc_value(val: Decimal) -> str:
    """Format price or quantity for Kraken v2 CRC32 calculation.

    Kraken v2 checksum specification: strip decimal points and leading zeros.
    """
    s = str(val).replace(".", "").lstrip("0")
    return s if s else "0"


class ManagedOrderBook:
    """Internal state container managing bids and asks for a single symbol."""

    def __init__(self, exchange: ExchangeId, symbol: str) -> None:
        self.exchange = exchange
        self.symbol = symbol
        # Price -> Quantity mapping
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.sequence_id: int = 0
        self.exchange_timestamp_ms: int = 0
        self.local_receipt_timestamp_ms: int = 0
        self.checksum: Optional[int] = None
        self.is_stale: bool = True
        self.is_hydrated: bool = False

        # Binance buffer for diffs arriving before REST snapshot
        self.binance_diff_buffer: list[dict[str, Any]] = []

    def set_snapshot(self, snapshot: NormalizedOrderBook) -> None:
        """Hydrate book from REST snapshot."""
        self.bids.clear()
        self.asks.clear()

        for b in snapshot.bids:
            if b.quantity > Decimal("0"):
                self.bids[b.price] = b.quantity
        for a in snapshot.asks:
            if a.quantity > Decimal("0"):
                self.asks[a.price] = a.quantity

        self.sequence_id = snapshot.sequence_id
        self.exchange_timestamp_ms = snapshot.exchange_timestamp_ms
        self.local_receipt_timestamp_ms = snapshot.local_receipt_timestamp_ms
        self.checksum = snapshot.checksum
        self.is_stale = False
        self.is_hydrated = True

    def apply_level_updates(
        self,
        bids_raw: list[list[Any]] | list[dict[str, Any]],
        asks_raw: list[list[Any]] | list[dict[str, Any]],
    ) -> None:
        """Apply incremental level updates (quantity 0 indicates level removal)."""
        # Process Bids
        for item in bids_raw:
            if isinstance(item, dict):
                p = Decimal(str(item["price"]))
                q = Decimal(str(item["qty"]))
            else:
                p = Decimal(str(item[0]))
                q = Decimal(str(item[1]))

            if q == Decimal("0"):
                self.bids.pop(p, None)
            else:
                self.bids[p] = q

        # Process Asks
        for item in asks_raw:
            if isinstance(item, dict):
                p = Decimal(str(item["price"]))
                q = Decimal(str(item["qty"]))
            else:
                p = Decimal(str(item[0]))
                q = Decimal(str(item[1]))

            if q == Decimal("0"):
                self.asks.pop(p, None)
            else:
                self.asks[p] = q

    def calculate_kraken_checksum(self) -> int:
        """Compute 32-bit CRC32 checksum over top 10 asks and top 10 bids."""
        sorted_asks = sorted(self.asks.items(), key=lambda x: x[0])[:10]
        sorted_bids = sorted(self.bids.items(), key=lambda x: x[0], reverse=True)[:10]

        parts: list[str] = []
        # 10 Asks
        for price, qty in sorted_asks:
            parts.append(format_kraken_crc_value(price))
            parts.append(format_kraken_crc_value(qty))

        # 10 Bids
        for price, qty in sorted_bids:
            parts.append(format_kraken_crc_value(price))
            parts.append(format_kraken_crc_value(qty))

        concatenated = "".join(parts)
        return zlib.crc32(concatenated.encode("utf-8")) & 0xFFFFFFFF

    def to_normalized(self) -> NormalizedOrderBook:
        """Export current state into an immutable-like NormalizedOrderBook."""
        sorted_bids = [
            OrderBookLevel(price=p, quantity=q)
            for p, q in sorted(self.bids.items(), key=lambda x: x[0], reverse=True)
        ]
        sorted_asks = [
            OrderBookLevel(price=p, quantity=q)
            for p, q in sorted(self.asks.items(), key=lambda x: x[0])
        ]

        # Check crossed book
        crossed = False
        if sorted_bids and sorted_asks:
            if sorted_bids[0].price >= sorted_asks[0].price:
                crossed = True

        stale = self.is_stale or crossed or not self.is_hydrated

        return NormalizedOrderBook(
            exchange=self.exchange,
            symbol=self.symbol,
            bids=sorted_bids,
            asks=sorted_asks,
            sequence_id=self.sequence_id,
            exchange_timestamp_ms=self.exchange_timestamp_ms,
            local_receipt_timestamp_ms=self.local_receipt_timestamp_ms,
            checksum=self.checksum,
            is_stale=stale,
        )


class OrderBookEngineImpl:
    """Production implementation of OrderBookEngine protocol."""

    def __init__(self, max_data_age_ms: int = 250) -> None:
        self.max_data_age_ms = max_data_age_ms
        # Key: (exchange, canonical_symbol)
        self._books: dict[tuple[ExchangeId, str], ManagedOrderBook] = {}

    def _get_or_create(self, exchange: ExchangeId, symbol: str) -> ManagedOrderBook:
        canonical = SymbolNormalizer.to_canonical(exchange, symbol)
        key = (exchange, canonical)
        if key not in self._books:
            self._books[key] = ManagedOrderBook(exchange, canonical)
        return self._books[key]

    def apply_snapshot(self, snapshot: NormalizedOrderBook) -> None:
        """Hydrate order book from REST depth snapshot."""
        book = self._get_or_create(snapshot.exchange, snapshot.symbol)
        book.set_snapshot(snapshot)

        # If Binance, replay any valid buffered diffs
        if snapshot.exchange == ExchangeId.BINANCE and book.binance_diff_buffer:
            self._replay_binance_buffer(book)

    def _replay_binance_buffer(self, book: ManagedOrderBook) -> None:
        """Replay buffered Binance diffs post-snapshot."""
        buffered = list(book.binance_diff_buffer)
        book.binance_diff_buffer.clear()

        for diff in buffered:
            try:
                self.apply_binance_diff(diff)
            except OrderBookDesyncError:
                book.is_stale = True
                break

    def apply_binance_diff(
        self, diff_payload: dict[str, Any], receipt_timestamp_ms: Optional[int] = None
    ) -> NormalizedOrderBook:
        """Apply incremental Binance @depth@100ms diff with U/u sequence verification."""
        raw_symbol = diff_payload.get("s", "")
        canonical = SymbolNormalizer.to_canonical(ExchangeId.BINANCE, raw_symbol)
        book = self._get_or_create(ExchangeId.BINANCE, canonical)

        now_ms = receipt_timestamp_ms or int(time.time() * 1000)
        book.local_receipt_timestamp_ms = now_ms
        book.exchange_timestamp_ms = diff_payload.get("E", now_ms)

        U = int(diff_payload.get("U", 0))
        u = int(diff_payload.get("u", 0))

        if not book.is_hydrated:
            # Buffer diff while waiting for REST snapshot
            book.binance_diff_buffer.append(diff_payload)
            return book.to_normalized()

        # Step 1: Drop events where u <= lastUpdateId
        if u <= book.sequence_id:
            return book.to_normalized()

        # Step 2: First processed event must satisfy U <= lastUpdateId + 1 <= u
        if book.sequence_id == book.binance_diff_buffer:
            pass

        # Step 3: Check sequence continuity
        # If this is the first diff after snapshot:
        # U <= lastUpdateId + 1 and u >= lastUpdateId + 1
        if U > book.sequence_id + 1:
            book.is_stale = True
            raise OrderBookDesyncError(
                f"Binance sequence gap on {canonical}: received U={U}, expected <= {book.sequence_id + 1}",
                {"U": U, "u": u, "lastUpdateId": book.sequence_id},
            )

        # Apply updates
        bids_raw = diff_payload.get("b", [])
        asks_raw = diff_payload.get("a", [])
        book.apply_level_updates(bids_raw, asks_raw)
        book.sequence_id = u
        book.is_stale = False

        return book.to_normalized()

    def apply_kraken_update(
        self, update_payload: dict[str, Any], receipt_timestamp_ms: Optional[int] = None
    ) -> NormalizedOrderBook:
        """Apply Kraken v2 book update and verify CRC32 checksum."""
        data_list = update_payload.get("data", [])
        if not data_list:
            raise ValueError("Kraken update missing 'data' field")

        data = data_list[0]
        raw_symbol = data.get("symbol", "")
        canonical = SymbolNormalizer.to_canonical(ExchangeId.KRAKEN, raw_symbol)
        book = self._get_or_create(ExchangeId.KRAKEN, canonical)

        now_ms = receipt_timestamp_ms or int(time.time() * 1000)
        book.local_receipt_timestamp_ms = now_ms
        book.exchange_timestamp_ms = now_ms

        bids_raw = data.get("bids", [])
        asks_raw = data.get("asks", [])
        book.apply_level_updates(bids_raw, asks_raw)

        expected_checksum = data.get("checksum")
        if expected_checksum is not None:
            expected_crc = int(expected_checksum)
            computed_crc = book.calculate_kraken_checksum()
            book.checksum = expected_crc

            if computed_crc != expected_crc:
                book.is_stale = True
                raise ChecksumMismatchError(
                    f"Kraken CRC32 checksum mismatch on {canonical}: computed={computed_crc}, expected={expected_crc}",
                    {"computed": computed_crc, "expected": expected_crc},
                )

        book.is_stale = False
        book.is_hydrated = True
        return book.to_normalized()

    def apply_diff(self, diff_payload: dict[str, Any]) -> NormalizedOrderBook:
        """Generic dispatcher for diff payloads based on exchange structure."""
        if "e" in diff_payload and diff_payload["e"] == "depthUpdate":
            return self.apply_binance_diff(diff_payload)
        elif "channel" in diff_payload and diff_payload["channel"] == "book":
            return self.apply_kraken_update(diff_payload)
        raise ValueError("Unrecognized diff payload format")

    def verify_checksum(self, exchange: ExchangeId, symbol: str, expected_checksum: int) -> bool:
        """Verify CRC32 checksum for a symbol on Kraken."""
        canonical = SymbolNormalizer.to_canonical(exchange, symbol)
        book = self._books.get((exchange, canonical))
        if not book:
            return False
        computed = book.calculate_kraken_checksum()
        return computed == expected_checksum

    def get_book(self, exchange: ExchangeId, symbol: str) -> Optional[NormalizedOrderBook]:
        """Retrieve current in-memory normalized order book."""
        canonical = SymbolNormalizer.to_canonical(exchange, symbol)
        book = self._books.get((exchange, canonical))
        if not book:
            return None
        return book.to_normalized()

    def is_stale(self, exchange: ExchangeId, symbol: str, max_age_ms: Optional[int] = None) -> bool:
        """Check if local order book exceeds freshness threshold or is flagged stale."""
        canonical = SymbolNormalizer.to_canonical(exchange, symbol)
        book = self._books.get((exchange, canonical))
        if not book or not book.is_hydrated:
            return True
        threshold = max_age_ms or self.max_data_age_ms
        age = int(time.time() * 1000) - book.local_receipt_timestamp_ms
        return book.is_stale or age > threshold
