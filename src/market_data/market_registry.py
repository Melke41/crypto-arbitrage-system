"""Market registry for symbol filters, precision rules, and trading constraints.

Provides thread-safe in-memory caching and querying of SymbolMetadata.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from src.core.models import ExchangeId, SymbolMetadata
from src.market_data.normalizer import SymbolNormalizer


class MarketRegistry:
    """Registry maintaining active trading pair filters and fee specifications."""

    def __init__(self) -> None:
        # Key: (exchange, canonical_symbol)
        self._registry: dict[tuple[ExchangeId, str], SymbolMetadata] = {}

    def register(self, metadata: SymbolMetadata) -> None:
        """Register or update a symbol's constraints in the registry."""
        key = (metadata.exchange, metadata.symbol)
        self._registry[key] = metadata

    def get(self, exchange: ExchangeId, symbol: str) -> Optional[SymbolMetadata]:
        """Retrieve metadata for a symbol on a specific exchange."""
        canonical = SymbolNormalizer.to_canonical(exchange, symbol)
        return self._registry.get((exchange, canonical))

    def get_all(self, exchange: Optional[ExchangeId] = None) -> list[SymbolMetadata]:
        """Return all registered symbol metadata, optionally filtered by exchange."""
        if exchange is None:
            return list(self._registry.values())
        return [meta for (ex, _), meta in self._registry.items() if ex == exchange]

    def clear(self) -> None:
        """Clear all registered symbols."""
        self._registry.clear()

    @staticmethod
    def parse_binance_symbol_info(
        raw_info: dict[str, Any], default_maker_fee: Decimal = Decimal("0.001"), default_taker_fee: Decimal = Decimal("0.001")
    ) -> SymbolMetadata:
        """Parse Binance /api/v3/exchangeInfo symbol object into SymbolMetadata."""
        native = raw_info["symbol"]
        canonical = SymbolNormalizer.to_canonical(ExchangeId.BINANCE, native)
        base = raw_info["baseAsset"]
        quote = raw_info["quoteAsset"]

        tick_size = Decimal("0.01")
        lot_size = Decimal("0.0001")
        min_qty = Decimal("0.0001")
        max_qty = None
        min_notional = Decimal("5.0")

        for f in raw_info.get("filters", []):
            filter_type = f.get("filterType")
            if filter_type == "PRICE_FILTER":
                tick_size = Decimal(str(f.get("tickSize", "0.01")))
            elif filter_type == "LOT_SIZE":
                lot_size = Decimal(str(f.get("stepSize", "0.0001")))
                min_qty = Decimal(str(f.get("minQty", "0.0001")))
                if "maxQty" in f:
                    max_qty = Decimal(str(f["maxQty"]))
            elif filter_type in ("NOTIONAL", "MIN_NOTIONAL"):
                min_notional = Decimal(str(f.get("minNotional", "5.0")))

        return SymbolMetadata(
            exchange=ExchangeId.BINANCE,
            symbol=canonical,
            native_symbol=native,
            base_asset=base,
            quote_asset=quote,
            tick_size=tick_size,
            lot_size=lot_size,
            min_qty=min_qty,
            max_qty=max_qty,
            min_notional=min_notional,
            maker_fee_rate=default_maker_fee,
            taker_fee_rate=default_taker_fee,
        )

    @staticmethod
    def parse_kraken_asset_pair(
        pair_name: str,
        raw_pair: dict[str, Any],
        default_maker_fee: Decimal = Decimal("0.0025"),
        default_taker_fee: Decimal = Decimal("0.0040"),
    ) -> SymbolMetadata:
        """Parse Kraken /0/public/AssetPairs entry into SymbolMetadata."""
        canonical = SymbolNormalizer.to_canonical(ExchangeId.KRAKEN, pair_name)
        base = raw_pair.get("base", "")
        quote = raw_pair.get("quote", "")

        # Lot decimals and tick size
        pair_decimals = int(raw_pair.get("pair_decimals", 2))
        lot_decimals = int(raw_pair.get("lot_decimals", 8))

        tick_size = Decimal("1") / (Decimal("10") ** pair_decimals)
        lot_size = Decimal("1") / (Decimal("10") ** lot_decimals)

        ordermin = Decimal(str(raw_pair.get("ordermin", str(lot_size))))
        costmin = Decimal(str(raw_pair.get("costmin", "5.0")))

        return SymbolMetadata(
            exchange=ExchangeId.KRAKEN,
            symbol=canonical,
            native_symbol=raw_pair.get("wsname", pair_name),
            base_asset=SymbolNormalizer.KRAKEN_ASSET_MAP.get(base, base),
            quote_asset=SymbolNormalizer.KRAKEN_ASSET_MAP.get(quote, quote),
            tick_size=tick_size,
            lot_size=lot_size,
            min_qty=ordermin,
            max_qty=None,
            min_notional=costmin,
            maker_fee_rate=default_maker_fee,
            taker_fee_rate=default_taker_fee,
        )
