"""Account balance tracking and atomic inventory reservation.

All financial balances are stored in exact Decimal.
Thread-safe and async-safe locking ensures concurrent opportunities cannot
double-spend the same pre-funded inventory.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Optional

from src.core.models import (
    BalanceRecord,
    ExchangeId,
    OrderSide,
)
from src.core.exceptions import InsufficientBalanceError


class BalanceManager:
    """Manages multi-exchange asset balances with atomic reservation and settlement."""

    def __init__(self) -> None:
        # Key: (ExchangeId, asset_symbol) -> BalanceRecord
        self._balances: dict[tuple[ExchangeId, str], BalanceRecord] = {}
        self._lock = asyncio.Lock()

    def set_balance(
        self,
        exchange: ExchangeId,
        asset: str,
        free: Decimal,
        locked: Decimal = Decimal("0"),
    ) -> BalanceRecord:
        """Set or update the balance for a specific exchange and asset."""
        asset_clean = asset.upper()
        total = free + locked
        record = BalanceRecord(
            exchange=exchange,
            asset=asset_clean,
            free=free,
            locked=locked,
            total=total,
            updated_at_ms=int(time.time() * 1000),
        )
        self._balances[(exchange, asset_clean)] = record
        return record

    def get_balance(self, exchange: ExchangeId, asset: str) -> BalanceRecord:
        """Retrieve balance record, returning zeroed record if asset untracked."""
        asset_clean = asset.upper()
        record = self._balances.get((exchange, asset_clean))
        if record is None:
            return BalanceRecord(
                exchange=exchange,
                asset=asset_clean,
                free=Decimal("0"),
                locked=Decimal("0"),
                total=Decimal("0"),
                updated_at_ms=int(time.time() * 1000),
            )
        return record

    def get_all_balances(self) -> list[BalanceRecord]:
        """Return list of all current balance records."""
        return list(self._balances.values())

    async def lock_funds(self, exchange: ExchangeId, asset: str, amount: Decimal) -> bool:
        """Atomically reserve funds from free to locked balance.

        Returns True if successful, False if free balance is insufficient.
        """
        if amount <= Decimal("0"):
            return True

        async with self._lock:
            current = self.get_balance(exchange, asset)
            if current.free < amount:
                return False

            new_free = current.free - amount
            new_locked = current.locked + amount
            self.set_balance(exchange, asset, free=new_free, locked=new_locked)
            return True

    async def release_locked(self, exchange: ExchangeId, asset: str, amount: Decimal) -> bool:
        """Atomically release locked funds back to free balance."""
        if amount <= Decimal("0"):
            return True

        async with self._lock:
            current = self.get_balance(exchange, asset)
            release_qty = min(amount, current.locked)
            new_free = current.free + release_qty
            new_locked = current.locked - release_qty
            self.set_balance(exchange, asset, free=new_free, locked=new_locked)
            return True

    async def settle_fill(
        self,
        exchange: ExchangeId,
        base_asset: str,
        quote_asset: str,
        side: OrderSide,
        filled_qty: Decimal,
        fill_price: Decimal,
        fee: Decimal = Decimal("0"),
        fee_asset: Optional[str] = None,
    ) -> None:
        """Atomically settle a filled order.

        - BUY: Deduct locked quote currency (notional), add delivered base currency.
        - SELL: Deduct locked base currency (quantity), add delivered quote currency.
        - Deduct fee from appropriate asset balance.
        """
        async with self._lock:
            base_clean = base_asset.upper()
            quote_clean = quote_asset.upper()
            notional = filled_qty * fill_price

            if side == OrderSide.BUY:
                # Deduct quote from locked
                quote_rec = self.get_balance(exchange, quote_clean)
                quote_locked = max(Decimal("0"), quote_rec.locked - notional)
                self.set_balance(exchange, quote_clean, free=quote_rec.free, locked=quote_locked)

                # Add base to free (deduct fee if fee is in base asset)
                delivered_base = filled_qty
                if fee_asset and fee_asset.upper() == base_clean:
                    delivered_base -= fee

                base_rec = self.get_balance(exchange, base_clean)
                self.set_balance(
                    exchange,
                    base_clean,
                    free=base_rec.free + delivered_base,
                    locked=base_rec.locked,
                )

            elif side == OrderSide.SELL:
                # Deduct base from locked
                base_rec = self.get_balance(exchange, base_clean)
                base_locked = max(Decimal("0"), base_rec.locked - filled_qty)
                self.set_balance(exchange, base_clean, free=base_rec.free, locked=base_locked)

                # Add quote to free (deduct fee if fee is in quote asset)
                delivered_quote = notional
                if fee_asset and fee_asset.upper() == quote_clean:
                    delivered_quote -= fee

                quote_rec = self.get_balance(exchange, quote_clean)
                self.set_balance(
                    exchange,
                    quote_clean,
                    free=quote_rec.free + delivered_quote,
                    locked=quote_rec.locked,
                )

            # If fee asset is independent (e.g. BNB), deduct fee from free balance of that asset
            if fee_asset:
                fee_clean = fee_asset.upper()
                if fee_clean != base_clean and fee_clean != quote_clean and fee > Decimal("0"):
                    fee_rec = self.get_balance(exchange, fee_clean)
                    new_free = max(Decimal("0"), fee_rec.free - fee)
                    self.set_balance(exchange, fee_clean, free=new_free, locked=fee_rec.locked)
