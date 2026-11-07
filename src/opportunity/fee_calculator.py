"""Fee calculation engine for Spot and Perpetual Futures across Binance and Kraken.

Enforces conservative fee defaults, BNB fee discount rules, and exact
base-asset vs quote-asset fee accounting.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from src.core.models import ExchangeId, InstrumentType


class FeeCalculator:
    """Computes exact transaction fees for spot and derivatives orders."""

    # Default conservative fee tiers (VIP 0)
    # Binance Spot default (without BNB): 0.10% (10 bps) maker, 0.10% (10 bps) taker
    BINANCE_SPOT_MAKER_DEFAULT = Decimal("0.0010")
    BINANCE_SPOT_TAKER_DEFAULT = Decimal("0.0010")

    # Binance Spot with BNB discount: 0.075% (7.5 bps)
    BINANCE_SPOT_MAKER_BNB = Decimal("0.00075")
    BINANCE_SPOT_TAKER_BNB = Decimal("0.00075")

    # Binance USD-M Perpetual Futures: 0.02% (2 bps) maker, 0.05% (5 bps) taker
    BINANCE_PERP_MAKER = Decimal("0.0002")
    BINANCE_PERP_TAKER = Decimal("0.0005")

    # Kraken Spot default: 0.25% (25 bps) maker, 0.40% (40 bps) taker
    KRAKEN_SPOT_MAKER = Decimal("0.0025")
    KRAKEN_SPOT_TAKER = Decimal("0.0040")

    # Kraken Futures: 0.02% (2 bps) maker, 0.05% (5 bps) taker
    KRAKEN_PERP_MAKER = Decimal("0.0002")
    KRAKEN_PERP_TAKER = Decimal("0.0005")

    @classmethod
    def get_fee_rates(
        cls,
        exchange: ExchangeId,
        instrument_type: InstrumentType = InstrumentType.SPOT,
        enable_bnb_discount: bool = False,
        bnb_balance: Decimal = Decimal("0"),
    ) -> tuple[Decimal, Decimal]:
        """Return (maker_fee_rate, taker_fee_rate) for the given venue and instrument."""
        if exchange == ExchangeId.BINANCE:
            if instrument_type == InstrumentType.SPOT:
                # BNB discount only applies if explicitly enabled AND balance >= 0.1 BNB
                if enable_bnb_discount and bnb_balance >= Decimal("0.1"):
                    return cls.BINANCE_SPOT_MAKER_BNB, cls.BINANCE_SPOT_TAKER_BNB
                return cls.BINANCE_SPOT_MAKER_DEFAULT, cls.BINANCE_SPOT_TAKER_DEFAULT
            elif instrument_type in (InstrumentType.LINEAR_PERP, InstrumentType.INVERSE_PERP):
                return cls.BINANCE_PERP_MAKER, cls.BINANCE_PERP_TAKER

        elif exchange == ExchangeId.KRAKEN:
            if instrument_type == InstrumentType.SPOT:
                return cls.KRAKEN_SPOT_MAKER, cls.KRAKEN_SPOT_TAKER
            elif instrument_type in (InstrumentType.LINEAR_PERP, InstrumentType.INVERSE_PERP):
                return cls.KRAKEN_PERP_MAKER, cls.KRAKEN_PERP_TAKER

        # Safe fallback
        return Decimal("0.0010"), Decimal("0.0010")

    @classmethod
    def calculate_spot_buy_fee(
        cls,
        notional_cost: Decimal,
        quantity: Decimal,
        taker_fee_rate: Decimal,
        deduct_in_base: bool = True,
    ) -> tuple[Decimal, Decimal, Decimal]:
        """Calculate fee for a Spot BUY order.

        Args:
            notional_cost: Price * Quantity in quote currency (USDT).
            quantity: Requested base quantity.
            taker_fee_rate: Taker fee fraction (e.g. 0.001).
            deduct_in_base: If True (Binance Spot default when BNB is off),
                fee is deducted directly from received base asset.

        Returns:
            tuple: (fee_in_quote, fee_in_base, net_delivered_base_qty)
        """
        if deduct_in_base:
            fee_in_base = quantity * taker_fee_rate
            net_delivered_base = quantity - fee_in_base
            # In quote currency PnL terms, the fee is valued at the execution price
            fee_in_quote = notional_cost * taker_fee_rate
            return fee_in_quote, fee_in_base, net_delivered_base
        else:
            fee_in_quote = notional_cost * taker_fee_rate
            return fee_in_quote, Decimal("0"), quantity

    @classmethod
    def calculate_spot_sell_fee(
        cls,
        gross_proceeds: Decimal,
        taker_fee_rate: Decimal,
    ) -> Decimal:
        """Calculate fee for a Spot SELL order in quote currency."""
        return gross_proceeds * taker_fee_rate

    @classmethod
    def calculate_perp_fee(
        cls,
        notional: Decimal,
        taker_fee_rate: Decimal,
    ) -> Decimal:
        """Calculate fee for a Linear Perpetual Futures order (settled in quote margin asset)."""
        return notional * taker_fee_rate

    @classmethod
    def calculate_round_trip_cost_bps(
        cls,
        leg_fee_rates: list[Decimal],
    ) -> Decimal:
        """Sum total taker fees across all trade hops in basis points."""
        total_rate = sum(leg_fee_rates)
        return total_rate * Decimal("10000")
