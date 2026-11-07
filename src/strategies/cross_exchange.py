"""Strategy 1: Cross-Exchange Spot Arbitrage Engine.

Detects and evaluates simultaneous buy-low / sell-high opportunities across
Binance Spot and Kraken Spot using pre-funded inventory and depth-walked VWAP.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

from src.core.models import (
    ArbitrageOpportunity,
    ExchangeId,
    NormalizedOrderBook,
    StrategyType,
)
from src.market_data.market_registry import MarketRegistry
from src.opportunity.evaluator import OpportunityEvaluator
from src.opportunity.profitability import ProfitabilityCalculator
from src.strategies.base_strategy import BaseStrategy

logger = logging.getLogger(__name__)


class CrossExchangeStrategy(BaseStrategy):
    """Strategy 1: Binance Spot vs. Kraken Spot Pre-Funded Arbitrage."""

    def __init__(
        self,
        market_registry: MarketRegistry,
        evaluator: Optional[OpportunityEvaluator] = None,
        max_notional_per_trade: Decimal = Decimal("1000.0"),
        min_net_edge_bps: Decimal = Decimal("15.0"),
    ) -> None:
        super().__init__(
            strategy_type=StrategyType.CROSS_EXCHANGE,
            name="CROSS_EXCHANGE_SPOT",
            market_registry=market_registry,
            evaluator=evaluator,
        )
        self.max_notional = max_notional_per_trade
        self.min_net_edge_bps = min_net_edge_bps

        # Cache of latest NormalizedOrderBook: (exchange, canonical_symbol)
        self._books: dict[tuple[ExchangeId, str], NormalizedOrderBook] = {}

    def update_book(self, book: NormalizedOrderBook) -> None:
        """Store latest normalized order book in memory cache."""
        self._books[(book.exchange, book.symbol)] = book

    async def process_orderbook_update(
        self, book: NormalizedOrderBook
    ) -> list[ArbitrageOpportunity]:
        """Evaluate cross-exchange arbitrage upon order book update."""
        self.update_book(book)
        detected: list[ArbitrageOpportunity] = []

        symbol = book.symbol
        current_exchange = book.exchange
        counter_exchange = (
            ExchangeId.KRAKEN if current_exchange == ExchangeId.BINANCE else ExchangeId.BINANCE
        )

        counter_book = self._books.get((counter_exchange, symbol))
        if not counter_book:
            return detected

        meta_curr = self.registry.get(current_exchange, symbol)
        meta_count = self.registry.get(counter_exchange, symbol)

        if not meta_curr or not meta_count:
            return detected

        # Direction 1: Buy on current_exchange, Sell on counter_exchange
        opp_1 = ProfitabilityCalculator.evaluate_cross_exchange(
            book_buy=book,
            book_sell=counter_book,
            meta_buy=meta_curr,
            meta_sell=meta_count,
            target_notional=self.max_notional,
            min_net_edge_bps=self.min_net_edge_bps,
        )
        if opp_1:
            if self.evaluator:
                validated = await self.evaluator.evaluate_and_emit(opp_1, [book, counter_book])
                if validated:
                    detected.append(validated)
            else:
                detected.append(opp_1)

        # Direction 2: Buy on counter_exchange, Sell on current_exchange
        opp_2 = ProfitabilityCalculator.evaluate_cross_exchange(
            book_buy=counter_book,
            book_sell=book,
            meta_buy=meta_count,
            meta_sell=meta_curr,
            target_notional=self.max_notional,
            min_net_edge_bps=self.min_net_edge_bps,
        )
        if opp_2:
            if self.evaluator:
                validated = await self.evaluator.evaluate_and_emit(opp_2, [counter_book, book])
                if validated:
                    detected.append(validated)
            else:
                detected.append(opp_2)

        return detected
