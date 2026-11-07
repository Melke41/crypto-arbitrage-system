"""Quantitative performance analytics and risk-adjusted metrics engine.

Computes exact Decimal financial metrics (PnL breakdown, opportunity conversion funnel,
Sharpe/Sortino ratios, maximum drawdown, and strategy segmentation).
Zero pandas on the hot trading path; used strictly for post-trade and offline analytics.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field

from src.core.models import (
    ArbitrageOpportunity,
    ExecutionLegStatus,
    ExecutionPlan,
    ExecutionPlanStatus,
    HedgedPosition,
    OpportunityLifecycleState,
    OrderSide,
    StrategyType,
)


class PnLBreakdown(BaseModel):
    """Granular PnL component breakdown denominated in quote currency (USDT)."""

    model_config = ConfigDict(frozen=True)

    gross_profit: Decimal = Field(default=Decimal("0"))
    total_trading_fees: Decimal = Field(default=Decimal("0"))
    total_slippage_unwind_costs: Decimal = Field(default=Decimal("0"))
    funding_pnl_accrued: Decimal = Field(default=Decimal("0"))
    net_realized_profit: Decimal = Field(default=Decimal("0"))


class OpportunityFunnel(BaseModel):
    """Conversion funnel tracking opportunities from detection to final settlement."""

    model_config = ConfigDict(frozen=True)

    total_theoretical_detected: int = 0
    total_executable_detected: int = 0
    total_risk_approved: int = 0
    total_risk_rejected: int = 0
    rejection_breakdown: dict[str, int] = Field(default_factory=dict)
    total_completed_plans: int = 0
    total_unwound_or_failed_plans: int = 0
    execution_failure_rate_pct: Decimal = Field(default=Decimal("0"))


class RiskAdjustedMetrics(BaseModel):
    """Standard quantitative risk and return performance metrics."""

    model_config = ConfigDict(frozen=True)

    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate_pct: Decimal = Field(default=Decimal("0"))
    profit_factor: Decimal = Field(default=Decimal("0"))
    avg_net_return_bps: Decimal = Field(default=Decimal("0"))
    max_drawdown_usdt: Decimal = Field(default=Decimal("0"))
    max_drawdown_pct: Decimal = Field(default=Decimal("0"))
    return_on_capital_pct: Decimal = Field(default=Decimal("0"))
    sharpe_ratio: Optional[Decimal] = None
    sortino_ratio: Optional[Decimal] = None


class StrategyPerformance(BaseModel):
    """Strategy-specific performance slice."""

    model_config = ConfigDict(frozen=True)

    strategy_type: StrategyType
    total_plans: int = 0
    completed_plans: int = 0
    unwound_plans: int = 0
    net_pnl_usdt: Decimal = Field(default=Decimal("0"))
    win_rate_pct: Decimal = Field(default=Decimal("0"))
    profit_factor: Decimal = Field(default=Decimal("0"))
    avg_net_edge_bps: Decimal = Field(default=Decimal("0"))


class PerformanceReport(BaseModel):
    """Unified quantitative performance report container."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    overall_pnl: PnLBreakdown
    funnel: OpportunityFunnel
    risk_metrics: RiskAdjustedMetrics
    strategy_breakdown: dict[str, StrategyPerformance] = Field(default_factory=dict)
    pair_breakdown: dict[str, dict[str, Any]] = Field(default_factory=dict)
    lifecycle_breakdown: dict[str, dict[str, Any]] = Field(default_factory=dict)
    initial_capital_usdt: Decimal = Field(default=Decimal("20000.0"))
    final_equity_usdt: Decimal = Field(default=Decimal("20000.0"))


class PerformanceAnalyzer:
    """Computes comprehensive quantitative performance and risk metrics."""

    @classmethod
    def analyze(
        cls,
        opportunities: list[ArbitrageOpportunity],
        plans: list[ExecutionPlan],
        hedged_positions: Optional[list[HedgedPosition]] = None,
        initial_capital_usdt: Decimal = Decimal("20000.0"),
    ) -> PerformanceReport:
        """Run full financial and statistical performance analysis over execution history."""
        positions = hedged_positions or []

        # 1. Funnel Metrics
        theo_count = sum(1 for o in opportunities if o.lifecycle_state == OpportunityLifecycleState.THEORETICAL_OPPORTUNITY)
        exec_count = sum(1 for o in opportunities if o.lifecycle_state == OpportunityLifecycleState.EXECUTABLE_OPPORTUNITY)
        approved_count = len(plans)
        rejected_count = max(0, exec_count - approved_count)

        completed_plans = [p for p in plans if p.status == ExecutionPlanStatus.COMPLETED]
        unwound_plans = [p for p in plans if p.status in (ExecutionPlanStatus.UNWOUND, ExecutionPlanStatus.FAILED)]

        fail_rate = Decimal("0")
        if plans:
            fail_rate = (Decimal(str(len(unwound_plans))) / Decimal(str(len(plans)))) * Decimal("100.0")

        funnel = OpportunityFunnel(
            total_theoretical_detected=theo_count,
            total_executable_detected=exec_count,
            total_risk_approved=approved_count,
            total_risk_rejected=rejected_count,
            total_completed_plans=len(completed_plans),
            total_unwound_or_failed_plans=len(unwound_plans),
            execution_failure_rate_pct=fail_rate,
        )

        # 2. PnL Components
        gross_profit = Decimal("0")
        total_fees = Decimal("0")
        total_slippage = Decimal("0")
        funding_pnl = sum((pos.realized_funding_pnl_quote for pos in positions), Decimal("0"))

        net_trade_pnls: list[Decimal] = []

        for p in plans:
            pnl = p.realized_pnl
            net_trade_pnls.append(pnl)
            if pnl > Decimal("0"):
                gross_profit += pnl

            for leg in p.legs:
                price = leg.average_fill_price or leg.order.price or Decimal("0")
                if leg.fee_asset == "USDT" or not leg.fee_asset:
                    total_fees += leg.fee_paid
                else:
                    total_fees += leg.fee_paid * price

                # Slippage relative to order price
                if leg.order.price and leg.average_fill_price and leg.filled_qty > Decimal("0"):
                    if leg.order.side == OrderSide.BUY:
                        slip = (leg.average_fill_price - leg.order.price) * leg.filled_qty
                    else:
                        slip = (leg.order.price - leg.average_fill_price) * leg.filled_qty
                    if slip > Decimal("0"):
                        total_slippage += slip

        total_net_pnl = sum(net_trade_pnls, Decimal("0")) + funding_pnl

        overall_pnl = PnLBreakdown(
            gross_profit=gross_profit,
            total_trading_fees=total_fees,
            total_slippage_unwind_costs=total_slippage,
            funding_pnl_accrued=funding_pnl,
            net_realized_profit=total_net_pnl,
        )

        # 3. Risk-Adjusted Statistical Metrics
        wins = [pnl for pnl in net_trade_pnls if pnl > Decimal("0")]
        losses = [pnl for pnl in net_trade_pnls if pnl < Decimal("0")]

        win_rate = Decimal("0")
        if net_trade_pnls:
            win_rate = (Decimal(str(len(wins))) / Decimal(str(len(net_trade_pnls)))) * Decimal("100.0")

        sum_wins = sum(wins, Decimal("0"))
        sum_losses = abs(sum(losses, Decimal("0")))
        profit_factor = Decimal("0")
        if sum_losses > Decimal("0"):
            profit_factor = sum_wins / sum_losses
        elif sum_wins > Decimal("0"):
            profit_factor = Decimal("999.0")  # Arbitrarily large if no losses

        # Average Return in bps
        avg_return_bps = Decimal("0")
        if plans:
            total_notional = sum((p.target_notional for p in plans if p.target_notional > Decimal("0")), Decimal("0"))
            if total_notional > Decimal("0"):
                avg_return_bps = (total_net_pnl / total_notional) * Decimal("10000.0")

        # Maximum Drawdown on chronological equity curve
        peak = initial_capital_usdt
        current_equity = initial_capital_usdt
        max_dd_usdt = Decimal("0")
        max_dd_pct = Decimal("0")

        for pnl in net_trade_pnls:
            current_equity += pnl
            if current_equity > peak:
                peak = current_equity
            dd = peak - current_equity
            if dd > max_dd_usdt:
                max_dd_usdt = dd
                max_dd_pct = (dd / peak) * Decimal("100.0") if peak > Decimal("0") else Decimal("0")

        return_on_cap = (total_net_pnl / initial_capital_usdt) * Decimal("100.0") if initial_capital_usdt > Decimal("0") else Decimal("0")

        # Sharpe & Sortino Ratios (per trade)
        sharpe: Optional[Decimal] = None
        sortino: Optional[Decimal] = None
        if len(net_trade_pnls) >= 2:
            float_pnls = [float(p) for p in net_trade_pnls]
            mean_pnl = sum(float_pnls) / len(float_pnls)
            var_pnl = sum((x - mean_pnl) ** 2 for x in float_pnls) / (len(float_pnls) - 1)
            std_pnl = math.sqrt(var_pnl) if var_pnl > 0 else 0.0

            if std_pnl > 0:
                sharpe = Decimal(str(round(mean_pnl / std_pnl, 4)))

            downside_pnl = [min(0.0, x) for x in float_pnls]
            downside_var = sum(x ** 2 for x in downside_pnl) / len(downside_pnl)
            downside_std = math.sqrt(downside_var) if downside_var > 0 else 0.0

            if downside_std > 0:
                sortino = Decimal(str(round(mean_pnl / downside_std, 4)))

        risk_metrics = RiskAdjustedMetrics(
            total_trades=len(net_trade_pnls),
            winning_trades=len(wins),
            losing_trades=len(losses),
            win_rate_pct=win_rate,
            profit_factor=profit_factor,
            avg_net_return_bps=avg_return_bps,
            max_drawdown_usdt=max_dd_usdt,
            max_drawdown_pct=max_dd_pct,
            return_on_capital_pct=return_on_cap,
            sharpe_ratio=sharpe,
            sortino_ratio=sortino,
        )

        # 4. Strategy Segmentation Breakdown
        strategy_breakdown: dict[str, StrategyPerformance] = {}
        for st in StrategyType:
            st_plans = [p for p in plans if p.strategy_type == st]
            if not st_plans:
                continue
            st_completed = [p for p in st_plans if p.status == ExecutionPlanStatus.COMPLETED]
            st_unwound = [p for p in st_plans if p.status in (ExecutionPlanStatus.UNWOUND, ExecutionPlanStatus.FAILED)]
            st_pnls = [p.realized_pnl for p in st_plans]
            st_wins = [pnl for pnl in st_pnls if pnl > Decimal("0")]
            st_losses = [pnl for pnl in st_pnls if pnl < Decimal("0")]

            st_win_rate = (Decimal(str(len(st_wins))) / Decimal(str(len(st_plans)))) * Decimal("100.0")
            st_sum_wins = sum(st_wins, Decimal("0"))
            st_sum_losses = abs(sum(st_losses, Decimal("0")))
            st_pf = Decimal("0")
            if st_sum_losses > Decimal("0"):
                st_pf = st_sum_wins / st_sum_losses
            elif st_sum_wins > Decimal("0"):
                st_pf = Decimal("999.0")

            st_notional = sum((p.target_notional for p in st_plans if p.target_notional > Decimal("0")), Decimal("0"))
            st_net_pnl = sum(st_pnls, Decimal("0"))
            st_avg_bps = (st_net_pnl / st_notional) * Decimal("10000.0") if st_notional > Decimal("0") else Decimal("0")

            strategy_breakdown[st.value] = StrategyPerformance(
                strategy_type=st,
                total_plans=len(st_plans),
                completed_plans=len(st_completed),
                unwound_plans=len(st_unwound),
                net_pnl_usdt=st_net_pnl,
                win_rate_pct=st_win_rate,
                profit_factor=st_pf,
                avg_net_edge_bps=st_avg_bps,
            )

        # 5. Lifecycle Segmentation Breakdown (Strict separation of Paper, Testnet, Live)
        lifecycle_breakdown: dict[str, dict[str, Any]] = {}
        for state in OpportunityLifecycleState:
            state_opps = [o for o in opportunities if o.lifecycle_state == state]
            lifecycle_breakdown[state.value] = {
                "opportunity_count": len(state_opps),
                "total_target_notional": str(sum((o.target_notional for o in state_opps), Decimal("0"))),
                "expected_profit_usdt": str(sum((o.expected_net_profit for o in state_opps), Decimal("0"))),
            }

        final_equity = initial_capital_usdt + total_net_pnl

        return PerformanceReport(
            overall_pnl=overall_pnl,
            funnel=funnel,
            risk_metrics=risk_metrics,
            strategy_breakdown=strategy_breakdown,
            pair_breakdown={},
            lifecycle_breakdown=lifecycle_breakdown,
            initial_capital_usdt=initial_capital_usdt,
            final_equity_usdt=final_equity,
        )
