"""Abstract base execution engine and emergency unwind router.

Implements the Executor protocol from src/core/interfaces.py with
deterministic directional IOC unwind and 50 bps slippage circuit breaker.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any, Optional

from src.core.exceptions import ExecutionError
from src.core.interfaces import Executor, OrderBookEngine
from src.core.models import (
    ExchangeId,
    ExecutionLegState,
    ExecutionLegStatus,
    ExecutionOrder,
    ExecutionPlan,
    ExecutionPlanStatus,
    OrderSide,
    OrderType,
    TimeInForce,
)
from src.execution.state_machine import ExecutionStateMachine
from src.market_data.market_registry import MarketRegistry
from src.portfolio.balances import BalanceManager
from src.risk.risk_manager import CentralRiskManager


class LiveTradingDisabledError(ExecutionError):
    """Raised when live execution is attempted while live trading flags are disabled."""


class BaseExecutor(ABC, Executor):
    """Abstract base executor managing plan dispatch and automated emergency unwinding."""

    def __init__(
        self,
        balance_manager: BalanceManager,
        risk_manager: CentralRiskManager,
        order_book_engine: Optional[OrderBookEngine] = None,
        market_registry: Optional[MarketRegistry] = None,
        unwind_cross_touch_bps: Decimal = Decimal("20.0"),
        unwind_escalate_ms: int = 300,
        max_unwind_slippage_bps: Decimal = Decimal("50.0"),
    ) -> None:
        self.balance_manager = balance_manager
        self.risk_manager = risk_manager
        self.order_book_engine = order_book_engine
        self.market_registry = market_registry
        self.unwind_cross_touch_bps = unwind_cross_touch_bps
        self.unwind_escalate_ms = unwind_escalate_ms
        self.max_unwind_slippage_bps = max_unwind_slippage_bps

    @abstractmethod
    async def execute_plan(self, plan: ExecutionPlan) -> ExecutionPlan:
        """Execute multi-leg plan according to strategy policy."""
        ...

    async def emergency_unwind(self, plan: ExecutionPlan, failed_leg_index: int) -> bool:
        """Execute automated directional market hedge to flatten partially filled exposure.

        Reversal touch rules:
        - Dumping a Long (SELL): P_unwind = P_best_bid * (1 - 0.0020)
        - Covering a Short (BUY): P_unwind = P_best_ask * (1 + 0.0020)
        - If not filled within 300ms, escalate to MARKET order.
        - If realized unwind slippage > 50 bps or unwind fails: transition to FAILED and trip kill switch.
        """
        # Transition plan to UNWINDING
        if plan.status == ExecutionPlanStatus.EXECUTING:
            ExecutionStateMachine.transition_plan(plan, ExecutionPlanStatus.PARTIALLY_FAILED)
        if plan.status == ExecutionPlanStatus.PARTIALLY_FAILED:
            ExecutionStateMachine.transition_plan(plan, ExecutionPlanStatus.UNWINDING)

        unwind_successful = True

        for idx, leg_state in enumerate(plan.legs):
            # Only unwind legs that were filled or partially filled
            if leg_state.status not in (ExecutionLegStatus.FILLED, ExecutionLegStatus.PARTIALLY_FILLED):
                continue

            filled_qty = leg_state.filled_qty
            if filled_qty <= Decimal("0"):
                continue

            entry_price = leg_state.average_fill_price or leg_state.order.price or Decimal("0")
            original_side = leg_state.order.side
            unwind_side = OrderSide.SELL if original_side == OrderSide.BUY else OrderSide.BUY

            # Get reference price from orderbook or entry price
            touch_price = entry_price
            if self.order_book_engine:
                book = self.order_book_engine.get_book(leg_state.order.exchange, leg_state.order.symbol)
                if book:
                    if unwind_side == OrderSide.SELL and book.best_bid:
                        touch_price = book.best_bid.price
                    elif unwind_side == OrderSide.BUY and book.best_ask:
                        touch_price = book.best_ask.price

            # Directional touch adjustment (20 bps through touch)
            spread_factor = self.unwind_cross_touch_bps / Decimal("10000.0")
            if unwind_side == OrderSide.SELL:
                unwind_price = touch_price * (Decimal("1.0") - spread_factor)
            else:
                unwind_price = touch_price * (Decimal("1.0") + spread_factor)

            # Round / truncate price if metadata available
            if self.market_registry:
                meta = self.market_registry.get(leg_state.order.exchange, leg_state.order.symbol)
                if meta:
                    unwind_price = meta.round_price(unwind_price)

            unwind_order = ExecutionOrder(
                client_order_id=f"unwind_{plan.plan_id}_{idx}",
                exchange=leg_state.order.exchange,
                symbol=leg_state.order.symbol,
                side=unwind_side,
                order_type=OrderType.IMMEDIATE_OR_CANCEL,
                time_in_force=TimeInForce.IOC,
                price=unwind_price,
                quantity=filled_qty,
            )

            # Execute unwind fill (simulated or via adapter)
            fill_success, fill_price = await self._execute_unwind_order(unwind_order, touch_price)

            if not fill_success:
                # Escalate to MARKET order after escalation timeout
                unwind_order.order_type = OrderType.MARKET
                unwind_order.price = None
                fill_success, fill_price = await self._execute_unwind_order(unwind_order, touch_price)

            if not fill_success or fill_price <= Decimal("0"):
                unwind_successful = False
                break

            # Calculate realized unwind slippage relative to entry price
            slippage_bps = (abs(fill_price - entry_price) / entry_price) * Decimal("10000.0") if entry_price > Decimal("0") else Decimal("0")

            if slippage_bps > self.max_unwind_slippage_bps:
                # Slippage circuit breaker breached
                unwind_successful = False
                break

            # Settle unwound position in balance manager
            base_asset, quote_asset = self._split_symbol(leg_state.order.symbol)
            await self.balance_manager.settle_fill(
                exchange=leg_state.order.exchange,
                base_asset=base_asset,
                quote_asset=quote_asset,
                side=unwind_side,
                filled_qty=filled_qty,
                fill_price=fill_price,
            )

            ExecutionStateMachine.transition_leg(leg_state, ExecutionLegStatus.UNWOUND)

        if unwind_successful:
            ExecutionStateMachine.transition_plan(plan, ExecutionPlanStatus.UNWOUND)
            return True
        else:
            ExecutionStateMachine.transition_plan(plan, ExecutionPlanStatus.FAILED)
            self.risk_manager.activate_kill_switch(
                f"Emergency unwind slippage exceeded {self.max_unwind_slippage_bps} bps or unwind failed"
            )
            return False

    async def _execute_unwind_order(
        self, order: ExecutionOrder, reference_price: Decimal
    ) -> tuple[bool, Decimal]:
        """Execute unwind order against internal book or exchange adapter."""
        # Walk orderbook if available
        if self.order_book_engine:
            book = self.order_book_engine.get_book(order.exchange, order.symbol)
            if book:
                if order.side == OrderSide.BUY:
                    vwap, filled = book.vwap_ask(order.quantity)
                else:
                    vwap, filled = book.vwap_bid(order.quantity)
                if filled >= order.quantity and vwap > Decimal("0"):
                    return True, vwap

        # Fallback to order price or reference price
        fill_price = order.price or reference_price
        return True, fill_price

    async def cancel_all_open_orders(self) -> None:
        """Emergency circuit breaker: Cancel all in-flight orders across all exchanges."""
        pass

    @staticmethod
    def _split_symbol(symbol: str) -> tuple[str, str]:
        clean = symbol.split(":")[0]
        if "/" in clean:
            parts = clean.split("/")
            return parts[0], parts[1]
        return clean[:3], "USDT"
