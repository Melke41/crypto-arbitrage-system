"""Deterministic historical event-replay backtester for all 4 strategies."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Optional, Union

from config.settings import SystemSettings
from src.analytics.performance import PerformanceAnalyzer, PerformanceReport
from src.core.models import (
    FundingRateSnapshot,
    NormalizedOrderBook,
)
from src.main import ArbitrageSystemOrchestrator

MarketEvent = Union[NormalizedOrderBook, FundingRateSnapshot]


class HistoricalReplayBacktester:
    """Historical event-replay simulation engine testing the full production trading pipeline."""

    def __init__(
        self,
        orchestrator: Optional[ArbitrageSystemOrchestrator] = None,
        initial_capital_usdt: Decimal = Decimal("20000.0"),
    ) -> None:
        self.orchestrator = orchestrator or ArbitrageSystemOrchestrator(
            settings=SystemSettings(execution_mode="PAPER")
        )
        self.initial_capital_usdt = initial_capital_usdt

    async def run(self, events: list[MarketEvent]) -> PerformanceReport:
        """Deterministically replay ordered market events through the autonomous orchestrator."""
        await self.orchestrator.start()

        # Sort events chronologically by receipt timestamp
        sorted_events = sorted(events, key=self._extract_timestamp)

        for event in sorted_events:
            if isinstance(event, NormalizedOrderBook):
                await self.orchestrator.handle_orderbook_update(event)
            elif isinstance(event, FundingRateSnapshot):
                await self.orchestrator.handle_funding_update(event)

        await self.orchestrator.stop()

        # Extract generated artifacts from orchestrator
        opportunities = list(self.orchestrator._recent_opportunities)
        plans = list(self.orchestrator._recent_executions)
        positions = list(self.orchestrator.position_manager._positions.values())

        report = PerformanceAnalyzer.analyze(
            opportunities=opportunities,
            plans=plans,
            hedged_positions=positions,
            initial_capital_usdt=self.initial_capital_usdt,
        )
        return report

    @staticmethod
    def _extract_timestamp(event: MarketEvent) -> int:
        if isinstance(event, NormalizedOrderBook):
            return event.local_receipt_timestamp_ms or event.exchange_timestamp_ms or 0
        elif isinstance(event, FundingRateSnapshot):
            return event.local_receipt_timestamp_ms or 0
        return 0
