"""Risk limits configuration models and constants."""

from __future__ import annotations

from decimal import Decimal
from pydantic import BaseModel, ConfigDict, Field

from src.core.models import StrategyType


class RiskLimits(BaseModel):
    """Institutional pre-trade risk parameter thresholds."""

    model_config = ConfigDict(frozen=True)

    max_notional_per_trade: Decimal = Field(default=Decimal("1000.0"))
    max_daily_loss_usdt: Decimal = Field(default=Decimal("100.0"))
    max_drawdown_pct: Decimal = Field(default=Decimal("5.0"))
    max_open_plans: int = Field(default=3)
    max_leverage: int = Field(default=2)
    min_liquidation_buffer_pct: Decimal = Field(default=Decimal("35.0"))
    max_delta_drift_pct: Decimal = Field(default=Decimal("1.0"))
    max_data_age_ms: int = Field(default=250)
    circuit_breaker_cooldown_sec: int = Field(default=300)

    # Minimum net edge required per strategy type
    min_net_edge_cross_exchange_bps: Decimal = Field(default=Decimal("15.0"))
    min_net_edge_triangular_bps: Decimal = Field(default=Decimal("10.0"))
    min_net_edge_funding_basis_bps: Decimal = Field(default=Decimal("3.0"))
    min_net_edge_stat_arb_bps: Decimal = Field(default=Decimal("10.0"))

    def get_min_net_edge_bps(self, strategy_type: StrategyType) -> Decimal:
        """Lookup required minimum net edge for strategy type."""
        if strategy_type in (StrategyType.CROSS_EXCHANGE, StrategyType.CROSS_EXCHANGE_SPOT):
            return self.min_net_edge_cross_exchange_bps
        elif strategy_type in (StrategyType.TRIANGULAR, StrategyType.TRIANGULAR_SPOT):
            return self.min_net_edge_triangular_bps
        elif strategy_type == StrategyType.FUNDING_BASIS:
            return self.min_net_edge_funding_basis_bps
        elif strategy_type == StrategyType.STAT_ARB:
            return self.min_net_edge_stat_arb_bps
        return Decimal("10.0")
