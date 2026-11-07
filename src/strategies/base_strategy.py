"""Abstract Base Strategy Engine.

Provides common market registry access, staleness validation, and lifecycle methods.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional

from src.core.interfaces import StrategyEngine
from src.core.models import (
    ArbitrageOpportunity,
    NormalizedOrderBook,
    StrategyType,
)
from src.market_data.market_registry import MarketRegistry
from src.opportunity.evaluator import OpportunityEvaluator

logger = logging.getLogger(__name__)


class BaseStrategy(ABC):
    """Abstract base class conforming to StrategyEngine protocol."""

    def __init__(
        self,
        strategy_type: StrategyType,
        name: str,
        market_registry: MarketRegistry,
        evaluator: Optional[OpportunityEvaluator] = None,
    ) -> None:
        self._strategy_type = strategy_type
        self._name = name
        self.registry = market_registry
        self.evaluator = evaluator

    @property
    def strategy_type(self) -> StrategyType:
        """Return strategy classification."""
        return self._strategy_type

    @property
    def name(self) -> str:
        """Return human-readable strategy name."""
        return self._name

    @abstractmethod
    async def process_orderbook_update(
        self, book: NormalizedOrderBook
    ) -> list[ArbitrageOpportunity]:
        """Evaluate orderbook update and emit any detected opportunities."""
        pass

    def reset(self) -> None:
        """Reset internal caches or cycle graph state."""
        pass
