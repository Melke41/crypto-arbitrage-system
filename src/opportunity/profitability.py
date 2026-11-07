"""Profitability Engine for Cross-Exchange, Triangular, Funding Basis, and StatArb.

Calculates multi-level L2 VWAP depth execution, fee compounding, lot truncation,
and distinguishes theoretical from executable net edge.
"""

from __future__ import annotations

import time
import uuid
from decimal import Decimal
from typing import Any, Optional

from src.core.models import (
    ArbitrageLeg,
    ArbitrageOpportunity,
    FundingRateSnapshot,
    InstrumentType,
    NormalizedOrderBook,
    OpportunityLifecycleState,
    OrderSide,
    StatArbAction,
    StatArbSignal,
    StrategyType,
    SymbolMetadata,
)
from src.opportunity.fee_calculator import FeeCalculator


class ProfitabilityCalculator:
    """Computes exact depth-walked net profitability and edge in basis points."""

    # ==============================================================================
    # Strategy 1: Cross-Exchange Spot Arbitrage
    # ==============================================================================

    @classmethod
    def evaluate_cross_exchange(
        cls,
        book_buy: NormalizedOrderBook,
        book_sell: NormalizedOrderBook,
        meta_buy: SymbolMetadata,
        meta_sell: SymbolMetadata,
        target_notional: Decimal,
        min_net_edge_bps: Decimal = Decimal("15.0"),
    ) -> Optional[ArbitrageOpportunity]:
        """Evaluate arbitrage between buy venue (lower ask) and sell venue (higher bid)."""
        best_ask = book_buy.best_ask
        best_bid = book_sell.best_bid

        if not best_ask or not best_bid:
            return None

        # Theoretical top-of-book gross edge
        gross_spread = best_bid.price - best_ask.price
        if gross_spread <= Decimal("0"):
            return None

        gross_edge_bps = (gross_spread / best_ask.price) * Decimal("10000")

        # Determine target base quantity from target notional
        approx_qty = target_notional / best_ask.price

        # Harmonize quantity across both exchange step-size and filter grids
        ok, exec_qty, err = SymbolMetadata.match_cross_exchange_quantity(
            meta_buy=meta_buy,
            meta_sell=meta_sell,
            target_quantity=approx_qty,
            buy_price=best_ask.price,
            sell_price=best_bid.price,
        )
        if not ok or exec_qty <= Decimal("0"):
            return None

        # Multi-level L2 depth walk
        vwap_buy, fill_buy = book_buy.vwap_ask(exec_qty)
        vwap_sell, fill_sell = book_sell.vwap_bid(exec_qty)

        if fill_buy < exec_qty or fill_sell < exec_qty:
            # Insufficient executable depth
            return None

        gross_cost = exec_qty * vwap_buy
        gross_proceeds = exec_qty * vwap_sell

        # Calculate fees
        fee_buy_quote, _, _ = FeeCalculator.calculate_spot_buy_fee(
            notional_cost=gross_cost,
            quantity=exec_qty,
            taker_fee_rate=meta_buy.taker_fee_rate,
            deduct_in_base=False,  # Quote valuation of fee
        )
        fee_sell_quote = FeeCalculator.calculate_spot_sell_fee(
            gross_proceeds=gross_proceeds,
            taker_fee_rate=meta_sell.taker_fee_rate,
        )

        total_fees = fee_buy_quote + fee_sell_quote
        net_profit = gross_proceeds - gross_cost - total_fees
        net_edge_bps = (net_profit / gross_cost) * Decimal("10000")

        # Determine lifecycle state
        state = (
            OpportunityLifecycleState.EXECUTABLE_OPPORTUNITY
            if net_edge_bps >= min_net_edge_bps and net_profit > Decimal("0")
            else OpportunityLifecycleState.THEORETICAL_OPPORTUNITY
        )

        leg1 = ArbitrageLeg(
            leg_index=0,
            exchange=meta_buy.exchange,
            symbol=meta_buy.symbol,
            side=OrderSide.BUY,
            expected_price=vwap_buy,
            expected_qty=exec_qty,
            expected_fee=fee_buy_quote,
            fee_asset=meta_buy.quote_asset,
        )
        leg2 = ArbitrageLeg(
            leg_index=1,
            exchange=meta_sell.exchange,
            symbol=meta_sell.symbol,
            side=OrderSide.SELL,
            expected_price=vwap_sell,
            expected_qty=exec_qty,
            expected_fee=fee_sell_quote,
            fee_asset=meta_sell.quote_asset,
        )

        return ArbitrageOpportunity(
            id=f"opp-cross-{uuid.uuid4().hex[:12]}",
            strategy_type=StrategyType.CROSS_EXCHANGE,
            lifecycle_state=state,
            symbol_legs=[meta_buy.symbol],
            legs=[leg1, leg2],
            gross_edge_bps=gross_edge_bps,
            net_edge_bps=net_edge_bps,
            target_notional=gross_cost,
            expected_net_profit=net_profit,
            profit_asset=meta_buy.quote_asset,
            metadata={
                "buy_exchange": meta_buy.exchange.value,
                "sell_exchange": meta_sell.exchange.value,
                "vwap_buy": str(vwap_buy),
                "vwap_sell": str(vwap_sell),
                "exec_qty": str(exec_qty),
            },
        )

    # ==============================================================================
    # Strategy 2: Single-Exchange Triangular Arbitrage
    # ==============================================================================

    @classmethod
    def execute_triangular_hop(
        cls,
        input_amount: Decimal,
        from_asset: str,
        to_asset: str,
        book: NormalizedOrderBook,
        meta: SymbolMetadata,
    ) -> tuple[bool, Decimal, Decimal, Decimal, OrderSide, Optional[str]]:
        """Compute one hop in a 3-leg cycle.

        Returns:
            tuple: (success, output_amount, vwap_price, executed_qty, side, error)
        """
        # Determine if BUY or SELL
        if meta.quote_asset == from_asset and meta.base_asset == to_asset:
            # BUY base with quote
            side = OrderSide.BUY
            if not book.best_ask:
                return False, Decimal("0"), Decimal("0"), Decimal("0"), side, "No asks available"

            approx_qty = input_amount / book.best_ask.price
            exec_qty = meta.truncate_amount(approx_qty)
            if exec_qty < meta.min_qty:
                return False, Decimal("0"), Decimal("0"), Decimal("0"), side, f"Qty {exec_qty} < min_qty {meta.min_qty}"

            vwap, filled = book.vwap_ask(exec_qty)
            if filled < exec_qty:
                return False, Decimal("0"), Decimal("0"), Decimal("0"), side, "Insufficient ask depth"

            notional = exec_qty * vwap
            if notional < meta.min_notional:
                return False, Decimal("0"), Decimal("0"), Decimal("0"), side, f"Notional {notional} < min_notional"

            # Base delivered net of fee
            fee_in_base = exec_qty * meta.taker_fee_rate
            output_amount = exec_qty - fee_in_base
            return True, output_amount, vwap, exec_qty, side, None

        elif meta.base_asset == from_asset and meta.quote_asset == to_asset:
            # SELL base for quote
            side = OrderSide.SELL
            if not book.best_bid:
                return False, Decimal("0"), Decimal("0"), Decimal("0"), side, "No bids available"

            exec_qty = meta.truncate_amount(input_amount)
            if exec_qty < meta.min_qty:
                return False, Decimal("0"), Decimal("0"), Decimal("0"), side, f"Qty {exec_qty} < min_qty {meta.min_qty}"

            vwap, filled = book.vwap_bid(exec_qty)
            if filled < exec_qty:
                return False, Decimal("0"), Decimal("0"), Decimal("0"), side, "Insufficient bid depth"

            gross_proceeds = exec_qty * vwap
            if gross_proceeds < meta.min_notional:
                return False, Decimal("0"), Decimal("0"), Decimal("0"), side, f"Notional {gross_proceeds} < min_notional"

            # Quote proceeds net of fee
            fee_in_quote = gross_proceeds * meta.taker_fee_rate
            output_amount = gross_proceeds - fee_in_quote
            return True, output_amount, vwap, exec_qty, side, None

        return False, Decimal("0"), Decimal("0"), Decimal("0"), OrderSide.BUY, f"Pair {meta.symbol} does not connect {from_asset}->{to_asset}"

    # ==============================================================================
    # Strategy 3: Delta-Neutral Funding Rate & Basis Arbitrage
    # ==============================================================================

    @classmethod
    def evaluate_funding_basis(
        cls,
        spot_book: NormalizedOrderBook,
        perp_book: NormalizedOrderBook,
        funding_snapshot: FundingRateSnapshot,
        meta_spot: SymbolMetadata,
        meta_perp: SymbolMetadata,
        target_notional: Decimal,
        min_funding_bps_per_8h: Decimal = Decimal("3.0"),
        min_annualized_yield_pct: Decimal = Decimal("15.0"),
    ) -> Optional[ArbitrageOpportunity]:
        """Evaluate Delta-Neutral Carry: Long Spot + Short Perpetual."""
        best_spot_ask = spot_book.best_ask
        best_perp_bid = perp_book.best_bid

        if not best_spot_ask or not best_perp_bid:
            return None

        # Check funding rate threshold
        # Funding rate fraction -> bps per interval: current_funding_rate * 10,000
        funding_bps_8h = funding_snapshot.current_funding_rate * Decimal("10000")
        ann_yield_pct = funding_snapshot.annualized_funding_yield_pct

        if funding_bps_8h < min_funding_bps_per_8h or ann_yield_pct < min_annualized_yield_pct:
            return None

        # Harmonize quantity
        approx_qty = target_notional / best_spot_ask.price
        ok, exec_qty, err = SymbolMetadata.match_cross_exchange_quantity(
            meta_buy=meta_spot,
            meta_sell=meta_perp,
            target_quantity=approx_qty,
            buy_price=best_spot_ask.price,
            sell_price=best_perp_bid.price,
        )
        if not ok or exec_qty <= Decimal("0"):
            return None

        # Walk Spot Asks (Buy) and Perp Bids (Short)
        vwap_spot_buy, fill_spot = spot_book.vwap_ask(exec_qty)
        vwap_perp_sell, fill_perp = perp_book.vwap_bid(exec_qty)

        if fill_spot < exec_qty or fill_perp < exec_qty:
            return None

        spot_notional = exec_qty * vwap_spot_buy
        perp_notional = exec_qty * vwap_perp_sell

        # Entry Basis in bps: ((Perp - Spot) / Spot) * 10000
        entry_basis_bps = ((vwap_perp_sell - vwap_spot_buy) / vwap_spot_buy) * Decimal("10000")

        # Round-trip fees (Spot taker + Perp taker for entry and exit):
        # Entry fees
        spot_entry_fee = FeeCalculator.calculate_spot_sell_fee(spot_notional, meta_spot.taker_fee_rate)
        perp_entry_fee = FeeCalculator.calculate_perp_fee(perp_notional, meta_perp.taker_fee_rate)
        # Expected exit fees (approx equal to entry fees at same notional)
        total_round_trip_fees = (spot_entry_fee + perp_entry_fee) * Decimal("2")

        # Expected funding payout per 8h
        expected_funding_8h = perp_notional * funding_snapshot.current_funding_rate

        # Break-even holding period (in 8h intervals)
        # Net fee deficit to recover = total_round_trip_fees - (entry_basis_value)
        entry_basis_gain = perp_notional - spot_notional
        net_cost_to_recover = max(Decimal("0"), total_round_trip_fees - entry_basis_gain)

        intervals_to_breakeven = (
            net_cost_to_recover / expected_funding_8h
            if expected_funding_8h > Decimal("0")
            else Decimal("999")
        )
        hours_to_breakeven = intervals_to_breakeven * Decimal(str(funding_snapshot.funding_interval_hours))

        net_edge_bps = ann_yield_pct * Decimal("100")  # Proxy edge in annual bps

        leg_spot = ArbitrageLeg(
            leg_index=0,
            exchange=meta_spot.exchange,
            symbol=meta_spot.symbol,
            side=OrderSide.BUY,
            expected_price=vwap_spot_buy,
            expected_qty=exec_qty,
            expected_fee=spot_entry_fee,
            fee_asset=meta_spot.quote_asset,
        )
        leg_perp = ArbitrageLeg(
            leg_index=1,
            exchange=meta_perp.exchange,
            symbol=meta_perp.symbol,
            side=OrderSide.SELL,
            expected_price=vwap_perp_sell,
            expected_qty=exec_qty,
            expected_fee=perp_entry_fee,
            fee_asset="USDT",
        )

        return ArbitrageOpportunity(
            id=f"opp-funding-{uuid.uuid4().hex[:12]}",
            strategy_type=StrategyType.FUNDING_BASIS,
            lifecycle_state=OpportunityLifecycleState.EXECUTABLE_OPPORTUNITY,
            symbol_legs=[meta_spot.symbol, meta_perp.symbol],
            legs=[leg_spot, leg_perp],
            gross_edge_bps=entry_basis_bps,
            net_edge_bps=net_edge_bps,
            target_notional=spot_notional,
            expected_net_profit=expected_funding_8h,
            profit_asset="USDT",
            metadata={
                "funding_rate_8h_bps": str(funding_bps_8h),
                "annualized_yield_pct": str(ann_yield_pct),
                "entry_basis_bps": str(entry_basis_bps),
                "hours_to_breakeven": str(round(float(hours_to_breakeven), 1)),
                "vwap_spot_buy": str(vwap_spot_buy),
                "vwap_perp_sell": str(vwap_perp_sell),
            },
        )

    # ==============================================================================
    # Strategy 4: Statistical Arbitrage (Pairs Trading)
    # ==============================================================================

    @classmethod
    def evaluate_stat_arb(
        cls,
        signal: StatArbSignal,
        book_a: NormalizedOrderBook,
        book_b: NormalizedOrderBook,
        meta_a: SymbolMetadata,
        meta_b: SymbolMetadata,
        notional_per_leg: Decimal = Decimal("500.0"),
    ) -> Optional[ArbitrageOpportunity]:
        """Convert StatArbSignal into two-leg execution ArbitrageOpportunity."""
        if signal.action not in (StatArbAction.ENTER_LONG_SPREAD, StatArbAction.ENTER_SHORT_SPREAD):
            return None

        # Long Spread: Buy Asset A, Sell Asset B * beta
        # Short Spread: Sell Asset A, Buy Asset B * beta
        if signal.action == StatArbAction.ENTER_LONG_SPREAD:
            side_a = OrderSide.BUY
            side_b = OrderSide.SELL
            price_a = book_a.best_ask.price if book_a.best_ask else None
            price_b = book_b.best_bid.price if book_b.best_bid else None
        else:
            side_a = OrderSide.SELL
            side_b = OrderSide.BUY
            price_a = book_a.best_bid.price if book_a.best_bid else None
            price_b = book_b.best_ask.price if book_b.best_ask else None

        if not price_a or not price_b:
            return None

        qty_a = meta_a.truncate_amount(notional_per_leg / price_a)
        # Sizing B = (notional_per_leg * beta) / price_b
        raw_qty_b = (notional_per_leg * signal.hedge_ratio_beta) / price_b
        qty_b = meta_b.truncate_amount(raw_qty_b)

        if qty_a < meta_a.min_qty or qty_b < meta_b.min_qty:
            return None

        vwap_a, fill_a = book_a.vwap_ask(qty_a) if side_a == OrderSide.BUY else book_a.vwap_bid(qty_a)
        vwap_b, fill_b = book_b.vwap_bid(qty_b) if side_b == OrderSide.SELL else book_b.vwap_ask(qty_b)

        if fill_a < qty_a or fill_b < qty_b:
            return None

        notional_a = qty_a * vwap_a
        notional_b = qty_b * vwap_b

        fee_a = notional_a * meta_a.taker_fee_rate
        fee_b = notional_b * meta_b.taker_fee_rate

        # Estimated mean-reversion edge: |Z - Z_exit| * sigma_spread in bps
        spread_reversion_pct = abs(float(signal.z_score) - 0.4) * float(signal.spread_std) * 100.0
        est_edge_bps = Decimal(str(round(spread_reversion_pct * 100, 2)))

        leg_a = ArbitrageLeg(
            leg_index=0,
            exchange=meta_a.exchange,
            symbol=meta_a.symbol,
            side=side_a,
            expected_price=vwap_a,
            expected_qty=qty_a,
            expected_fee=fee_a,
            fee_asset=meta_a.quote_asset,
        )
        leg_b = ArbitrageLeg(
            leg_index=1,
            exchange=meta_b.exchange,
            symbol=meta_b.symbol,
            side=side_b,
            expected_price=vwap_b,
            expected_qty=qty_b,
            expected_fee=fee_b,
            fee_asset=meta_b.quote_asset,
        )

        return ArbitrageOpportunity(
            id=f"opp-statarb-{uuid.uuid4().hex[:12]}",
            strategy_type=StrategyType.STAT_ARB,
            lifecycle_state=OpportunityLifecycleState.EXECUTABLE_OPPORTUNITY,
            symbol_legs=[meta_a.symbol, meta_b.symbol],
            legs=[leg_a, leg_b],
            gross_edge_bps=est_edge_bps,
            net_edge_bps=est_edge_bps - (meta_a.taker_fee_rate + meta_b.taker_fee_rate) * Decimal("10000"),
            target_notional=notional_a + notional_b,
            expected_net_profit=(est_edge_bps / Decimal("10000")) * (notional_a + notional_b),
            profit_asset="USDT",
            metadata={
                "pair_id": signal.pair_id,
                "z_score": str(signal.z_score),
                "beta": str(signal.hedge_ratio_beta),
                "action": signal.action.value,
            },
        )
