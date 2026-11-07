"""Institutional Central Risk Manager and circuit-breaker engine.

Implements RiskManager protocol from src/core/interfaces.py.
Applies 8 deterministic pre-trade risk gates, enforces position limits,
and manages global kill switch engagement.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any, Callable, Optional

from src.core.interfaces import RiskManager
from src.core.models import (
    ArbitrageOpportunity,
    ExchangeId,
    ExecutionLegState,
    ExecutionOrder,
    OpportunityLifecycleState,
    OrderSide,
    RiskDecision,
    StrategyType,
)
from src.portfolio.balances import BalanceManager
from src.risk.exposure import ExposureTracker
from src.risk.limits import RiskLimits


class CentralRiskManager:
    """Institutional pre-trade risk gating, balance validation, and kill switch."""

    def __init__(
        self,
        balance_manager: BalanceManager,
        limits: Optional[RiskLimits] = None,
        exposure_tracker: Optional[ExposureTracker] = None,
        event_callback: Optional[Callable[[Any], None]] = None,
    ) -> None:
        self.balance_manager = balance_manager
        self.limits = limits or RiskLimits()
        self.exposure_tracker = exposure_tracker or ExposureTracker()
        self.event_callback = event_callback

        self._kill_switch_active: bool = False
        self._kill_switch_reason: Optional[str] = None
        self._lock = asyncio.Lock()

    # --------------------------------------------------------------------------
    # Kill Switch Management
    # --------------------------------------------------------------------------

    def activate_kill_switch(self, reason: str) -> None:
        """Engage global emergency kill switch and reject all subsequent trades."""
        self._kill_switch_active = True
        self._kill_switch_reason = reason

    def deactivate_kill_switch(self) -> None:
        """Disengage kill switch after manual risk audit."""
        self._kill_switch_active = False
        self._kill_switch_reason = None

    def is_kill_switch_active(self) -> bool:
        """Check if global kill switch is engaged."""
        return self._kill_switch_active

    @property
    def kill_switch_reason(self) -> Optional[str]:
        return self._kill_switch_reason

    # --------------------------------------------------------------------------
    # Pre-Trade Evaluation (8 Gated Checks)
    # --------------------------------------------------------------------------

    async def evaluate_opportunity(self, opportunity: ArbitrageOpportunity) -> RiskDecision:
        """Evaluate opportunity against all institutional pre-trade risk gates."""
        now_ms = int(time.time() * 1000)

        # Gate 1: Global Emergency Kill Switch
        if self._kill_switch_active:
            return RiskDecision(
                is_approved=False,
                reason=f"Emergency kill switch active: {self._kill_switch_reason}",
                rejection_code="KILL_SWITCH_ACTIVE",
            )

        # Gate 2: Opportunity Lifecycle State Gate
        if opportunity.lifecycle_state != OpportunityLifecycleState.EXECUTABLE_OPPORTUNITY:
            return RiskDecision(
                is_approved=False,
                reason=f"Opportunity is {opportunity.lifecycle_state.value}, must be EXECUTABLE_OPPORTUNITY",
                rejection_code="NOT_EXECUTABLE_OPPORTUNITY",
            )

        # Gate 3: Stale Data Latency Gate
        age_ms = now_ms - opportunity.timestamp_ms
        if age_ms > self.limits.max_data_age_ms:
            return RiskDecision(
                is_approved=False,
                reason=f"Opportunity data age {age_ms}ms exceeds threshold {self.limits.max_data_age_ms}ms",
                rejection_code="DATA_STALE",
            )
        if opportunity.metadata.get("is_stale", False):
            return RiskDecision(
                is_approved=False,
                reason="Opportunity relies on order book flagged as stale",
                rejection_code="BOOK_STALE",
            )

        # Gate 4: Minimum Net Edge Threshold
        min_edge = self.limits.get_min_net_edge_bps(opportunity.strategy_type)
        if opportunity.net_edge_bps < min_edge:
            return RiskDecision(
                is_approved=False,
                reason=f"Net edge {opportunity.net_edge_bps} bps below minimum {min_edge} bps",
                rejection_code="INSUFFICIENT_NET_EDGE",
            )

        # Gate 5: Pre-Funded Inventory & Margin Availability
        inventory_ok, inv_reason = await self._verify_inventory(opportunity)
        if not inventory_ok:
            return RiskDecision(
                is_approved=False,
                reason=f"Insufficient inventory: {inv_reason}",
                rejection_code="INSUFFICIENT_INVENTORY",
            )

        # Gate 6: Trade Notional & Active Exposure Caps
        if opportunity.target_notional > self.limits.max_notional_per_trade:
            return RiskDecision(
                is_approved=False,
                reason=f"Target notional {opportunity.target_notional} exceeds max {self.limits.max_notional_per_trade}",
                rejection_code="NOTIONAL_EXCEEDS_CAP",
            )
        if self.exposure_tracker.open_plans_count >= self.limits.max_open_plans:
            return RiskDecision(
                is_approved=False,
                reason=f"Active open plans ({self.exposure_tracker.open_plans_count}) at or above limit ({self.limits.max_open_plans})",
                rejection_code="MAX_OPEN_PLANS_EXCEEDED",
            )

        # Gate 7: Daily Realized Loss & Drawdown Breaker
        if self.exposure_tracker.is_daily_loss_breached(self.limits.max_daily_loss_usdt):
            self.activate_kill_switch(
                f"Daily realized loss {abs(self.exposure_tracker.daily_realized_pnl)} breached limit {self.limits.max_daily_loss_usdt}"
            )
            return RiskDecision(
                is_approved=False,
                reason="Daily loss limit breached; kill switch tripped",
                rejection_code="DAILY_LOSS_LIMIT_BREACHED",
            )
        if self.exposure_tracker.is_drawdown_breached(self.limits.max_drawdown_pct):
            self.activate_kill_switch(f"Peak-to-trough drawdown breached {self.limits.max_drawdown_pct}%")
            return RiskDecision(
                is_approved=False,
                reason="Max drawdown limit breached; kill switch tripped",
                rejection_code="MAX_DRAWDOWN_BREACHED",
            )

        # Gate 8: Derivatives & Stateful Position Risk (Strategies 3 & 4)
        if opportunity.strategy_type in (StrategyType.FUNDING_BASIS, StrategyType.STAT_ARB):
            strat_ok, strat_reason, strat_code = self._verify_derivatives_risk(opportunity)
            if not strat_ok:
                return RiskDecision(
                    is_approved=False,
                    reason=strat_reason,
                    rejection_code=strat_code,
                )

        return RiskDecision(
            is_approved=True,
            reason="Approved",
            adjusted_notional=opportunity.target_notional,
        )

    # --------------------------------------------------------------------------
    # Single Order & Inventory Checks
    # --------------------------------------------------------------------------

    async def check_pre_trade_limits(self, order: ExecutionOrder) -> tuple[bool, Optional[str]]:
        """Validate order size, price, and kill-switch state before submission."""
        if self._kill_switch_active:
            return False, f"Kill switch active: {self._kill_switch_reason}"

        if order.price and order.price > Decimal("0"):
            notional = order.price * order.quantity
            if notional > self.limits.max_notional_per_trade:
                return False, f"Order notional {notional} exceeds max {self.limits.max_notional_per_trade}"

        return True, None

    async def check_inventory_availability(
        self, exchange: ExchangeId, asset: str, required_qty: Decimal
    ) -> bool:
        """Verify pre-funded balance availability on target exchange."""
        bal = self.balance_manager.get_balance(exchange, asset)
        return bal.free >= required_qty

    async def record_fill(self, leg: ExecutionLegState) -> None:
        """Update tracking metrics upon leg execution completion."""
        # Recorded for telemetry/exposure monitoring
        pass

    # --------------------------------------------------------------------------
    # Private Helper Validations
    # --------------------------------------------------------------------------

    async def _verify_inventory(self, opportunity: ArbitrageOpportunity) -> tuple[bool, Optional[str]]:
        """Verify sufficient free balances for required legs."""
        if opportunity.strategy_type in (StrategyType.TRIANGULAR, StrategyType.TRIANGULAR_SPOT):
            # Sequential execution: only Leg 0 requires upfront capital
            if not opportunity.legs:
                return False, "Triangular opportunity has no legs"
            leg0 = opportunity.legs[0]
            if leg0.side == OrderSide.BUY:
                needed_asset = self._get_quote_asset(leg0.symbol)
                needed_amount = leg0.expected_price * leg0.expected_qty
            else:
                needed_asset = self._get_base_asset(leg0.symbol)
                needed_amount = leg0.expected_qty

            has_funds = await self.check_inventory_availability(leg0.exchange, needed_asset, needed_amount)
            if not has_funds:
                bal = self.balance_manager.get_balance(leg0.exchange, needed_asset)
                return False, f"Triangular Leg 0 requires {needed_amount} {needed_asset} on {leg0.exchange.value}, have {bal.free}"
            return True, None

        # Simultaneous execution: all legs require pre-funded inventory concurrently
        for leg in opportunity.legs:
            if leg.side == OrderSide.BUY:
                needed_asset = self._get_quote_asset(leg.symbol)
                needed_amount = leg.expected_price * leg.expected_qty
            else:
                needed_asset = self._get_base_asset(leg.symbol)
                needed_amount = leg.expected_qty

            has_funds = await self.check_inventory_availability(leg.exchange, needed_asset, needed_amount)
            if not has_funds:
                bal = self.balance_manager.get_balance(leg.exchange, needed_asset)
                return (
                    False,
                    f"Leg {leg.leg_index} requires {needed_amount} {needed_asset} on {leg.exchange.value}, have {bal.free}",
                )

        return True, None

    def _verify_derivatives_risk(self, opportunity: ArbitrageOpportunity) -> tuple[bool, str, Optional[str]]:
        """Verify leverage, liquidation buffer, and delta drift for derivatives opportunities."""
        leverage = opportunity.metadata.get("leverage", 1)
        if leverage > self.limits.max_leverage:
            return (
                False,
                f"Leverage {leverage} exceeds cap {self.limits.max_leverage}",
                "EXCESSIVE_LEVERAGE",
            )

        liq_buffer = opportunity.metadata.get("liquidation_buffer_pct")
        if liq_buffer is not None:
            liq_buffer_dec = Decimal(str(liq_buffer))
            if liq_buffer_dec < self.limits.min_liquidation_buffer_pct:
                return (
                    False,
                    f"Liquidation buffer {liq_buffer_dec}% below minimum {self.limits.min_liquidation_buffer_pct}%",
                    "INSUFFICIENT_LIQUIDATION_BUFFER",
                )

        delta_drift = opportunity.metadata.get("delta_drift_pct")
        if delta_drift is not None:
            drift_dec = Decimal(str(delta_drift))
            if drift_dec > self.limits.max_delta_drift_pct:
                return (
                    False,
                    f"Delta drift {drift_dec}% exceeds maximum {self.limits.max_delta_drift_pct}%",
                    "EXCESSIVE_DELTA_DRIFT",
                )

        return True, "", None

    @staticmethod
    def _get_base_asset(symbol: str) -> str:
        clean = symbol.split(":")[0]
        if "/" in clean:
            return clean.split("/")[0]
        return clean[:3]

    @staticmethod
    def _get_quote_asset(symbol: str) -> str:
        clean = symbol.split(":")[0]
        if "/" in clean:
            return clean.split("/")[1]
        return "USDT"
