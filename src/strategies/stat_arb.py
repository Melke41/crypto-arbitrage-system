"""Strategy 4: Statistical Arbitrage (Cointegrated Pairs / Mean-Reversion) Engine.

Consumes synchronized mid-prices from RollingTimeSeriesBuffer, computes rolling OLS
hedge ratio beta, spread Z-score, and Ornstein-Uhlenbeck half-life to trigger
hedged mean-reversion entries, profit-taking exits, and structural stop-losses.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any, Optional

from src.core.models import (
    ArbitrageOpportunity,
    NormalizedOrderBook,
    StatArbAction,
    StatArbSignal,
    StrategyType,
)
from src.market_data.market_registry import MarketRegistry
from src.market_data.timeseries import RollingTimeSeriesBuffer
from src.opportunity.evaluator import OpportunityEvaluator
from src.opportunity.profitability import ProfitabilityCalculator
from src.strategies.base_strategy import BaseStrategy

logger = logging.getLogger(__name__)


class StatArbPairConfig:
    def __init__(
        self,
        pair_id: str,
        symbol_a: str,
        symbol_b: str,
        initial_beta: Decimal = Decimal("1.0"),
        z_score_entry: Decimal = Decimal("2.2"),
        z_score_exit: Decimal = Decimal("0.4"),
        z_score_stop: Decimal = Decimal("4.0"),
        min_half_life_sec: Decimal = Decimal("30.0"),
        max_half_life_sec: Decimal = Decimal("14400.0"),
    ) -> None:
        self.pair_id = pair_id
        self.symbol_a = symbol_a
        self.symbol_b = symbol_b
        self.beta = initial_beta
        self.z_entry = z_score_entry
        self.z_exit = z_score_exit
        self.z_stop = z_score_stop
        self.min_half_life = min_half_life_sec
        self.max_half_life = max_half_life_sec
        self.buffer = RollingTimeSeriesBuffer(maxlen=600)


class StatArbStrategy(BaseStrategy):
    """Strategy 4: Statistical Arbitrage & Cointegration Pairs Engine."""

    def __init__(
        self,
        market_registry: MarketRegistry,
        evaluator: Optional[OpportunityEvaluator] = None,
        pair_configs: Optional[list[StatArbPairConfig]] = None,
        notional_per_leg: Decimal = Decimal("500.0"),
    ) -> None:
        super().__init__(
            strategy_type=StrategyType.STAT_ARB,
            name="STAT_ARB_PAIRS",
            market_registry=market_registry,
            evaluator=evaluator,
        )
        self.notional_per_leg = notional_per_leg

        # Active pair configurations: pair_id -> StatArbPairConfig
        self._pairs: dict[str, StatArbPairConfig] = {}
        self._symbol_to_pairs: dict[str, list[StatArbPairConfig]] = {}
        self._books: dict[str, NormalizedOrderBook] = {}

        default_configs = pair_configs or [
            StatArbPairConfig("ETH_BTC_SPREAD", "ETH/USDT", "BTC/USDT", initial_beta=Decimal("0.055")),
            StatArbPairConfig("SOL_ETH_SPREAD", "SOL/USDT", "ETH/USDT", initial_beta=Decimal("0.065")),
        ]
        for cfg in default_configs:
            self.register_pair(cfg)

    def register_pair(self, cfg: StatArbPairConfig) -> None:
        """Register a cointegrated pair."""
        self._pairs[cfg.pair_id] = cfg
        self._symbol_to_pairs.setdefault(cfg.symbol_a, []).append(cfg)
        self._symbol_to_pairs.setdefault(cfg.symbol_b, []).append(cfg)

    def update_book(self, book: NormalizedOrderBook) -> None:
        """Cache latest order book."""
        self._books[book.symbol] = book

    async def process_orderbook_update(
        self, book: NormalizedOrderBook
    ) -> list[ArbitrageOpportunity]:
        """Process price update and evaluate spread for impacted pairs."""
        self.update_book(book)
        impacted_pairs = self._symbol_to_pairs.get(book.symbol, [])
        detected: list[ArbitrageOpportunity] = []

        now_ms = int(time.time() * 1000)

        for pair_cfg in impacted_pairs:
            book_a = self._books.get(pair_cfg.symbol_a)
            book_b = self._books.get(pair_cfg.symbol_b)

            if not book_a or not book_b:
                continue

            mid_a = book_a.mid_price
            mid_b = book_b.mid_price

            if not mid_a or not mid_b:
                continue

            # Update rolling ring buffer
            pair_cfg.buffer.append(now_ms, mid_a, mid_b)

            # Check if buffer has sufficient observations
            if not pair_cfg.buffer.is_ready(min_periods=30):
                continue

            # Calculate spread series, rolling mean and std
            spreads, mean_s, std_s, curr_s = pair_cfg.buffer.get_spread_series(pair_cfg.beta)
            z_score, _, _ = pair_cfg.buffer.calculate_z_score(pair_cfg.beta, curr_s)

            # Check half-life mean-reversion filter
            half_life = pair_cfg.buffer.estimate_half_life(pair_cfg.beta)
            if half_life is not None:
                if half_life < pair_cfg.min_half_life or half_life > pair_cfg.max_half_life:
                    # Outside mean-reversion speed window (likely structural break or noise)
                    continue

            # Signal classification
            action = StatArbAction.HOLD
            if z_score >= pair_cfg.z_stop or z_score <= -pair_cfg.z_stop:
                action = StatArbAction.STOP_LOSS_SPREAD
            elif z_score >= pair_cfg.z_entry:
                action = StatArbAction.ENTER_SHORT_SPREAD
            elif z_score <= -pair_cfg.z_entry:
                action = StatArbAction.ENTER_LONG_SPREAD
            elif abs(z_score) <= pair_cfg.z_exit:
                action = StatArbAction.EXIT_SPREAD

            if action in (StatArbAction.ENTER_LONG_SPREAD, StatArbAction.ENTER_SHORT_SPREAD):
                signal = StatArbSignal(
                    pair_id=pair_cfg.pair_id,
                    symbol_a=pair_cfg.symbol_a,
                    symbol_b=pair_cfg.symbol_b,
                    hedge_ratio_beta=pair_cfg.beta,
                    spread_value=curr_s,
                    spread_mean=mean_s,
                    spread_std=std_s,
                    z_score=z_score,
                    half_life_seconds=half_life,
                    action=action,
                    timestamp_ms=now_ms,
                )

                meta_a = self.registry.get(book_a.exchange, pair_cfg.symbol_a)
                meta_b = self.registry.get(book_b.exchange, pair_cfg.symbol_b)

                if meta_a and meta_b:
                    opp = ProfitabilityCalculator.evaluate_stat_arb(
                        signal=signal,
                        book_a=book_a,
                        book_b=book_b,
                        meta_a=meta_a,
                        meta_b=meta_b,
                        notional_per_leg=self.notional_per_leg,
                    )
                    if opp:
                        if self.evaluator:
                            validated = await self.evaluator.evaluate_and_emit(opp, [book_a, book_b])
                            if validated:
                                detected.append(validated)
                        else:
                            detected.append(opp)

        return detected
