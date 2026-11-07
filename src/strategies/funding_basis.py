"""Strategy 3: Delta-Neutral Funding-Rate & Basis Carry Arbitrage Engine.

Evaluates Long Spot + Short Linear Perpetual pairs to capture funding yield
and basis premium while hedging directional market risk.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

from src.core.models import (
    ArbitrageOpportunity,
    ExchangeId,
    FundingRateSnapshot,
    HedgedPosition,
    NormalizedOrderBook,
    StrategyType,
)
from src.market_data.market_registry import MarketRegistry
from src.opportunity.evaluator import OpportunityEvaluator
from src.opportunity.profitability import ProfitabilityCalculator
from src.strategies.base_strategy import BaseStrategy

logger = logging.getLogger(__name__)


class FundingBasisStrategy(BaseStrategy):
    """Strategy 3: Delta-Neutral Spot vs. Linear Perp Carry."""

    def __init__(
        self,
        market_registry: MarketRegistry,
        evaluator: Optional[OpportunityEvaluator] = None,
        max_notional_per_trade: Decimal = Decimal("1000.0"),
        min_funding_bps_per_8h: Decimal = Decimal("3.0"),
        min_annualized_yield_pct: Decimal = Decimal("15.0"),
        basis_convergence_target_bps: Decimal = Decimal("2.0"),
    ) -> None:
        super().__init__(
            strategy_type=StrategyType.FUNDING_BASIS,
            name="FUNDING_BASIS_CARRY",
            market_registry=market_registry,
            evaluator=evaluator,
        )
        self.max_notional = max_notional_per_trade
        self.min_funding_bps = min_funding_bps_per_8h
        self.min_ann_yield = min_annualized_yield_pct
        self.basis_target_bps = basis_convergence_target_bps

        self._spot_books: dict[tuple[ExchangeId, str], NormalizedOrderBook] = {}
        self._perp_books: dict[tuple[ExchangeId, str], NormalizedOrderBook] = {}
        self._funding_snapshots: dict[tuple[ExchangeId, str], FundingRateSnapshot] = {}

    def update_spot_book(self, book: NormalizedOrderBook) -> None:
        """Cache Spot order book."""
        clean_sym = book.symbol.split(":")[0]
        self._spot_books[(book.exchange, clean_sym)] = book

    def update_perp_book(self, book: NormalizedOrderBook) -> None:
        """Cache Perpetual Futures order book."""
        clean_sym = book.symbol.split(":")[0]
        self._perp_books[(book.exchange, clean_sym)] = book

    def update_funding_snapshot(self, snapshot: FundingRateSnapshot) -> None:
        """Cache funding rate and mark price snapshot."""
        # Clean symbol to canonical base/quote
        clean_sym = snapshot.symbol.split(":")[0]
        self._funding_snapshots[(snapshot.exchange, clean_sym)] = snapshot

    async def process_orderbook_update(
        self, book: NormalizedOrderBook
    ) -> list[ArbitrageOpportunity]:
        """Evaluate carry opportunities when an order book updates."""
        # Determine whether spot or perp book
        is_perp = ":" in book.symbol or "PERP" in book.symbol
        clean_symbol = book.symbol.split(":")[0]

        if is_perp:
            self.update_perp_book(book)
        else:
            self.update_spot_book(book)

        spot_book = self._spot_books.get((book.exchange, clean_symbol))
        perp_book = self._perp_books.get((book.exchange, clean_symbol))
        funding_snap = self._funding_snapshots.get((book.exchange, clean_symbol))

        if not spot_book or not perp_book or not funding_snap:
            return []

        meta_spot = self.registry.get(book.exchange, clean_symbol)
        # Suffix perp symbol if needed
        perp_symbol = f"{clean_symbol}:USDT"
        meta_perp = self.registry.get(book.exchange, perp_symbol) or meta_spot

        if not meta_spot or not meta_perp:
            return []

        opp = ProfitabilityCalculator.evaluate_funding_basis(
            spot_book=spot_book,
            perp_book=perp_book,
            funding_snapshot=funding_snap,
            meta_spot=meta_spot,
            meta_perp=meta_perp,
            target_notional=self.max_notional,
            min_funding_bps_per_8h=self.min_funding_bps,
            min_annualized_yield_pct=self.min_ann_yield,
        )

        if not opp:
            return []

        if self.evaluator:
            validated = await self.evaluator.evaluate_and_emit(opp, [spot_book, perp_book])
            return [validated] if validated else []

        return [opp]

    def should_exit_position(
        self, position: HedgedPosition, current_funding: FundingRateSnapshot
    ) -> tuple[bool, str]:
        """Check if an open funding carry position should be closed.

        Triggers:
        1. Funding rate flips negative (carry becomes a loss)
        2. Basis has converged to target
        """
        if current_funding.current_funding_rate <= Decimal("0"):
            return True, f"Negative funding rate ({current_funding.current_funding_rate})"

        if current_funding.basis_bps is not None and current_funding.basis_bps <= self.basis_target_bps:
            return True, f"Basis converged to {current_funding.basis_bps} bps (target <= {self.basis_target_bps} bps)"

        return False, "Hold"
