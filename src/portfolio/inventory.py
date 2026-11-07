"""Cross-exchange inventory tracking and skew balancing.

Computes distribution of inventory across venues to prevent severe capital imbalances.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from src.core.models import ExchangeId
from src.portfolio.balances import BalanceManager


class InventoryBalancer:
    """Monitors asset distribution across exchanges and enforces concentration limits."""

    def __init__(self, balance_manager: BalanceManager) -> None:
        self.balance_manager = balance_manager

    def calculate_skew(self, asset: str) -> dict[ExchangeId, Decimal]:
        """Compute the percentage share (0.0 to 100.0) of an asset held on each exchange.

        Returns:
            dict[ExchangeId, Decimal]: e.g. {ExchangeId.BINANCE: Decimal("60.0"), ExchangeId.KRAKEN: Decimal("40.0")}
        """
        asset_clean = asset.upper()
        b_binance = self.balance_manager.get_balance(ExchangeId.BINANCE, asset_clean).total
        b_kraken = self.balance_manager.get_balance(ExchangeId.KRAKEN, asset_clean).total
        total = b_binance + b_kraken

        if total <= Decimal("0"):
            return {
                ExchangeId.BINANCE: Decimal("50.0"),
                ExchangeId.KRAKEN: Decimal("50.0"),
            }

        skew_binance = (b_binance / total) * Decimal("100.0")
        skew_kraken = (b_kraken / total) * Decimal("100.0")
        return {
            ExchangeId.BINANCE: skew_binance,
            ExchangeId.KRAKEN: skew_kraken,
        }

    def check_skew_limit(
        self,
        asset: str,
        target_exchange: ExchangeId,
        delta: Decimal,
        max_skew_pct: Decimal = Decimal("85.0"),
    ) -> bool:
        """Check whether applying delta to target_exchange would breach the max_skew_pct limit.

        delta > 0 means increasing holding on target_exchange.
        delta < 0 means decreasing holding on target_exchange.
        """
        asset_clean = asset.upper()
        b_binance = self.balance_manager.get_balance(ExchangeId.BINANCE, asset_clean).total
        b_kraken = self.balance_manager.get_balance(ExchangeId.KRAKEN, asset_clean).total

        if target_exchange == ExchangeId.BINANCE:
            new_binance = max(Decimal("0"), b_binance + delta)
            new_kraken = b_kraken
        else:
            new_binance = b_binance
            new_kraken = max(Decimal("0"), b_kraken + delta)

        new_total = new_binance + new_kraken
        if new_total <= Decimal("0"):
            return True

        skew_binance = (new_binance / new_total) * Decimal("100.0")
        skew_kraken = (new_kraken / new_total) * Decimal("100.0")

        return skew_binance <= max_skew_pct and skew_kraken <= max_skew_pct

    def get_total_portfolio_value_quote(
        self, mark_prices: dict[str, Decimal], quote_currency: str = "USDT"
    ) -> Decimal:
        """Compute estimated total value of all holdings denominated in quote_currency."""
        quote_clean = quote_currency.upper()
        total_value = Decimal("0")
        all_balances = self.balance_manager.get_all_balances()

        for b in all_balances:
            if b.total <= Decimal("0"):
                continue
            if b.asset == quote_clean:
                total_value += b.total
            else:
                price = mark_prices.get(b.asset, mark_prices.get(f"{b.asset}/{quote_clean}", Decimal("0")))
                total_value += b.total * price

        return total_value
