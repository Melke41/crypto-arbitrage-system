"""Opportunity Evaluator.

Validates candidate arbitrage opportunities across all 4 strategies against
stale-data gates, crossed-book filters, and minimum edge requirements.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any, Callable, Coroutine, Optional

from src.core.events import OpportunityDetectedEvent
from src.core.models import (
    ArbitrageOpportunity,
    NormalizedOrderBook,
    OpportunityLifecycleState,
    StrategyType,
)

logger = logging.getLogger(__name__)


class OpportunityEvaluator:
    """Central gating and evaluation filter for quantitative arbitrage opportunities."""

    def __init__(
        self,
        max_data_age_ms: int = 250,
        min_net_edge_cross_bps: Decimal = Decimal("15.0"),
        min_net_edge_tri_bps: Decimal = Decimal("10.0"),
        on_opportunity_detected: Optional[
            Callable[[OpportunityDetectedEvent], Coroutine[Any, Any, None]]
        ] = None,
    ) -> None:
        self.max_data_age_ms = max_data_age_ms
        self.min_net_edge_cross_bps = min_net_edge_cross_bps
        self.min_net_edge_tri_bps = min_net_edge_tri_bps
        self.on_opportunity_detected = on_opportunity_detected

    def validate_book_freshness(
        self, book: NormalizedOrderBook, current_time_ms: Optional[int] = None
    ) -> tuple[bool, Optional[str]]:
        """Verify order book is not stale and not crossed."""
        if book.is_stale:
            return False, f"Order book {book.exchange.value}:{book.symbol} is flagged STALE"

        now_ms = current_time_ms or int(time.time() * 1000)
        age = now_ms - book.local_receipt_timestamp_ms
        if age > self.max_data_age_ms:
            return False, f"Order book {book.exchange.value}:{book.symbol} age {age}ms exceeds threshold {self.max_data_age_ms}ms"

        # Crossed book check
        if book.best_bid and book.best_ask:
            if book.best_bid.price >= book.best_ask.price:
                return False, f"Order book {book.exchange.value}:{book.symbol} is CROSSED ({book.best_bid.price} >= {book.best_ask.price})"

        return True, None

    async def evaluate_and_emit(
        self, opportunity: ArbitrageOpportunity, participating_books: list[NormalizedOrderBook]
    ) -> Optional[ArbitrageOpportunity]:
        """Validate opportunity against book integrity and emit event if valid."""
        now_ms = int(time.time() * 1000)

        # 1. Staleness and crossed-book check on all participating books
        for book in participating_books:
            valid, reason = self.validate_book_freshness(book, current_time_ms=now_ms)
            if not valid:
                logger.debug("Rejected opportunity %s: %s", opportunity.id, reason)
                return None

        # 2. Gate classification: EXECUTABLE vs THEORETICAL
        threshold = self.min_net_edge_cross_bps
        if opportunity.strategy_type in (StrategyType.TRIANGULAR, StrategyType.TRIANGULAR_SPOT):
            threshold = self.min_net_edge_tri_bps

        if opportunity.net_edge_bps >= threshold and opportunity.expected_net_profit > Decimal("0"):
            opportunity.lifecycle_state = OpportunityLifecycleState.EXECUTABLE_OPPORTUNITY
        else:
            opportunity.lifecycle_state = OpportunityLifecycleState.THEORETICAL_OPPORTUNITY

        # 3. Emit event to pipeline
        if self.on_opportunity_detected:
            await self.on_opportunity_detected(OpportunityDetectedEvent(opportunity=opportunity))

        return opportunity
