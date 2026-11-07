"""Strategy 2: Single-Exchange Triangular Arbitrage Engine.

Pre-computes directed 3-leg conversion cycles at initialization, indexes them
by participating symbols, and evaluates depth-walked compounded return on updates.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import NamedTuple, Optional

from src.core.models import (
    ArbitrageLeg,
    ArbitrageOpportunity,
    ExchangeId,
    NormalizedOrderBook,
    OpportunityLifecycleState,
    OrderSide,
    StrategyType,
    SymbolMetadata,
)
from src.market_data.market_registry import MarketRegistry
from src.opportunity.evaluator import OpportunityEvaluator
from src.opportunity.profitability import ProfitabilityCalculator
from src.strategies.base_strategy import BaseStrategy

logger = logging.getLogger(__name__)


class CycleHop(NamedTuple):
    from_asset: str
    to_asset: str
    symbol: str


class TriangularCycle(NamedTuple):
    cycle_id: str
    starting_asset: str
    hops: tuple[CycleHop, CycleHop, CycleHop]


class TriangularStrategy(BaseStrategy):
    """Strategy 2: Single-Exchange Directed 3-Leg Triangular Arbitrage."""

    def __init__(
        self,
        exchange: ExchangeId,
        market_registry: MarketRegistry,
        evaluator: Optional[OpportunityEvaluator] = None,
        max_notional_per_trade: Decimal = Decimal("1000.0"),
        min_net_edge_bps: Decimal = Decimal("10.0"),
        custom_cycles: Optional[list[list[str]]] = None,
    ) -> None:
        super().__init__(
            strategy_type=StrategyType.TRIANGULAR,
            name=f"TRIANGULAR_{exchange.value}",
            market_registry=market_registry,
            evaluator=evaluator,
        )
        self.exchange = exchange
        self.max_notional = max_notional_per_trade
        self.min_net_edge_bps = min_net_edge_bps

        # Books cache: symbol -> NormalizedOrderBook
        self._books: dict[str, NormalizedOrderBook] = {}

        # Pre-computed cycles and reverse lookup: symbol -> list[TriangularCycle]
        self._cycles: list[TriangularCycle] = []
        self._symbol_to_cycles: dict[str, list[TriangularCycle]] = {}

        self._build_cycles(custom_cycles)

    def _build_cycles(self, custom_cycles: Optional[list[list[str]]]) -> None:
        """Pre-compute directed cycles."""
        raw_cycles = custom_cycles or [
            ["USDT", "BTC", "ETH", "USDT"],
            ["USDT", "ETH", "BTC", "USDT"],
            ["USDT", "SOL", "BTC", "USDT"],
            ["USDT", "BNB", "BTC", "USDT"],
        ]

        for cycle_nodes in raw_cycles:
            if len(cycle_nodes) != 4 or cycle_nodes[0] != cycle_nodes[3]:
                continue

            a, b, c = cycle_nodes[0], cycle_nodes[1], cycle_nodes[2]
            hop1 = self._find_hop(a, b)
            hop2 = self._find_hop(b, c)
            hop3 = self._find_hop(c, a)

            if hop1 and hop2 and hop3:
                cycle_id = f"{self.exchange.value}-{a}->{b}->{c}->{a}"
                tri_cycle = TriangularCycle(
                    cycle_id=cycle_id,
                    starting_asset=a,
                    hops=(hop1, hop2, hop3),
                )
                self._cycles.append(tri_cycle)

                # Index by participating symbols for O(1) triggering
                for hop in tri_cycle.hops:
                    self._symbol_to_cycles.setdefault(hop.symbol, []).append(tri_cycle)

    def _find_hop(self, from_asset: str, to_asset: str) -> Optional[CycleHop]:
        """Determine symbol connecting two assets."""
        # Check Base/Quote or Quote/Base
        sym1 = f"{to_asset}/{from_asset}"
        if self.registry.get(self.exchange, sym1):
            return CycleHop(from_asset, to_asset, sym1)

        sym2 = f"{from_asset}/{to_asset}"
        if self.registry.get(self.exchange, sym2):
            return CycleHop(from_asset, to_asset, sym2)

        # Fallback heuristic
        return CycleHop(from_asset, to_asset, f"{from_asset}/{to_asset}")

    def update_book(self, book: NormalizedOrderBook) -> None:
        """Cache incoming order book."""
        if book.exchange == self.exchange:
            self._books[book.symbol] = book

    async def process_orderbook_update(
        self, book: NormalizedOrderBook
    ) -> list[ArbitrageOpportunity]:
        """Evaluate only the cycles containing the updated symbol."""
        if book.exchange != self.exchange:
            return []

        self.update_book(book)
        impacted_cycles = self._symbol_to_cycles.get(book.symbol, [])
        detected: list[ArbitrageOpportunity] = []

        for cycle in impacted_cycles:
            opp = await self._evaluate_cycle(cycle)
            if opp:
                detected.append(opp)

        return detected

    async def _evaluate_cycle(self, cycle: TriangularCycle) -> Optional[ArbitrageOpportunity]:
        """Simulate execution through all 3 hops."""
        current_amount = self.max_notional
        legs: list[ArbitrageLeg] = []
        participating_books: list[NormalizedOrderBook] = []

        for idx, hop in enumerate(cycle.hops):
            book = self._books.get(hop.symbol)
            meta = self.registry.get(self.exchange, hop.symbol)

            if not book or not meta:
                return None

            participating_books.append(book)

            # Execute single hop
            ok, next_amount, vwap, exec_qty, side, err = ProfitabilityCalculator.execute_triangular_hop(
                input_amount=current_amount,
                from_asset=hop.from_asset,
                to_asset=hop.to_asset,
                book=book,
                meta=meta,
            )
            if not ok or next_amount <= Decimal("0"):
                return None

            fee_rate = meta.taker_fee_rate
            fee_amount = (
                exec_qty * fee_rate if side == OrderSide.BUY else (exec_qty * vwap) * fee_rate
            )
            fee_asset = meta.base_asset if side == OrderSide.BUY else meta.quote_asset

            leg = ArbitrageLeg(
                leg_index=idx,
                exchange=self.exchange,
                symbol=hop.symbol,
                side=side,
                expected_price=vwap,
                expected_qty=exec_qty,
                expected_fee=fee_amount,
                fee_asset=fee_asset,
            )
            legs.append(leg)
            current_amount = next_amount

        # Calculate final net profit in starting asset
        final_capital = current_amount
        net_profit = final_capital - self.max_notional
        net_edge_bps = (net_profit / self.max_notional) * Decimal("10000")

        state = (
            OpportunityLifecycleState.EXECUTABLE_OPPORTUNITY
            if net_edge_bps >= self.min_net_edge_bps and net_profit > Decimal("0")
            else OpportunityLifecycleState.THEORETICAL_OPPORTUNITY
        )

        opp = ArbitrageOpportunity(
            id=f"opp-tri-{uuid.uuid4().hex[:12]}",
            strategy_type=StrategyType.TRIANGULAR,
            lifecycle_state=state,
            symbol_legs=[hop.symbol for hop in cycle.hops],
            legs=legs,
            gross_edge_bps=net_edge_bps + Decimal("20.0"),  # Approx gross before compounded fees
            net_edge_bps=net_edge_bps,
            target_notional=self.max_notional,
            expected_net_profit=net_profit,
            profit_asset=cycle.starting_asset,
            metadata={"cycle_id": cycle.cycle_id, "final_capital": str(final_capital)},
        )

        if self.evaluator:
            return await self.evaluator.evaluate_and_emit(opp, participating_books)
        return opp
