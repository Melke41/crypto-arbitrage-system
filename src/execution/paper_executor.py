"""Realistic paper execution engine with L2 order book depth walking and latency decay.

Simulates simultaneous and sequential execution plans with exact fee accounting,
base-asset fee deductions, and automated emergency unwinding.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Optional

from src.core.models import (
    ExchangeId,
    ExecutionLegState,
    ExecutionLegStatus,
    ExecutionOrder,
    ExecutionPlan,
    ExecutionPlanStatus,
    InstrumentType,
    OrderSide,
    OrderType,
    StrategyType,
    SymbolMetadata,
)
from src.core.interfaces import OrderBookEngine
from src.execution.base_executor import BaseExecutor
from src.execution.state_machine import ExecutionStateMachine
from src.market_data.market_registry import MarketRegistry
from src.opportunity.fee_calculator import FeeCalculator
from src.portfolio.balances import BalanceManager
from src.risk.risk_manager import CentralRiskManager


class PaperExecutor(BaseExecutor):
    """L2 paper execution simulator with latency decay and exact fee settlement."""

    def __init__(
        self,
        balance_manager: BalanceManager,
        risk_manager: CentralRiskManager,
        order_book_engine: Optional[OrderBookEngine] = None,
        market_registry: Optional[MarketRegistry] = None,
        fee_calculator: Optional[FeeCalculator] = None,
        simulated_latency_ms: int = 20,
        unwind_cross_touch_bps: Decimal = Decimal("20.0"),
        unwind_escalate_ms: int = 300,
        max_unwind_slippage_bps: Decimal = Decimal("50.0"),
    ) -> None:
        super().__init__(
            balance_manager=balance_manager,
            risk_manager=risk_manager,
            order_book_engine=order_book_engine,
            market_registry=market_registry,
            unwind_cross_touch_bps=unwind_cross_touch_bps,
            unwind_escalate_ms=unwind_escalate_ms,
            max_unwind_slippage_bps=max_unwind_slippage_bps,
        )
        self.fee_calculator = fee_calculator or FeeCalculator()
        self.simulated_latency_ms = simulated_latency_ms

    async def execute_plan(self, plan: ExecutionPlan) -> ExecutionPlan:
        """Execute all legs of an execution plan (simultaneous or sequential)."""
        ExecutionStateMachine.transition_plan(plan, ExecutionPlanStatus.EXECUTING)

        if plan.strategy_type in (StrategyType.TRIANGULAR, StrategyType.TRIANGULAR_SPOT):
            return await self._execute_triangular_sequential(plan)
        else:
            return await self._execute_simultaneous(plan)

    # --------------------------------------------------------------------------
    # Simultaneous Execution (Strategies 1, 3, 4)
    # --------------------------------------------------------------------------

    async def _execute_simultaneous(self, plan: ExecutionPlan) -> ExecutionPlan:
        """Dispatch legs concurrently using asyncio.gather."""
        # Pre-lock funds for all legs
        for leg_state in plan.legs:
            base_asset, quote_asset = self._split_symbol(leg_state.order.symbol)
            order = leg_state.order

            if order.side == OrderSide.BUY:
                lock_asset = quote_asset
                lock_amount = (order.price or Decimal("0")) * order.quantity
            else:
                lock_asset = base_asset
                lock_amount = order.quantity

            locked = await self.balance_manager.lock_funds(order.exchange, lock_asset, lock_amount)
            if not locked:
                ExecutionStateMachine.transition_leg(
                    leg_state,
                    ExecutionLegStatus.FAILED,
                    error_message=f"Could not lock {lock_amount} {lock_asset} on {order.exchange.value}",
                )
                ExecutionStateMachine.transition_plan(plan, ExecutionPlanStatus.FAILED)
                return plan

        # Execute legs in parallel
        results = await asyncio.gather(
            *[self._simulate_order_fill(leg_state) for leg_state in plan.legs],
            return_exceptions=True,
        )

        all_filled = True
        failed_idx = -1
        for idx, leg_state in enumerate(plan.legs):
            if leg_state.status != ExecutionLegStatus.FILLED:
                all_filled = False
                failed_idx = idx
                break

        if all_filled:
            self._compute_plan_pnl(plan)
            ExecutionStateMachine.transition_plan(plan, ExecutionPlanStatus.COMPLETED)
        else:
            # Trigger emergency unwind
            await self.emergency_unwind(plan, failed_leg_index=failed_idx)

        return plan

    # --------------------------------------------------------------------------
    # Sequential Execution (Strategy 2 Triangular)
    # --------------------------------------------------------------------------

    async def _execute_triangular_sequential(self, plan: ExecutionPlan) -> ExecutionPlan:
        """Execute 3-leg cycle sequentially, chaining lot-truncated output into next leg."""
        current_qty: Optional[Decimal] = None
        failed_idx = -1

        for idx, leg_state in enumerate(plan.legs):
            base_asset, quote_asset = self._split_symbol(leg_state.order.symbol)
            order = leg_state.order

            # For legs 1 and 2, adjust input quantity from previous leg fill
            if current_qty is not None:
                meta = self.market_registry.get(order.exchange, order.symbol) if self.market_registry else None
                if order.side == OrderSide.BUY:
                    ref_price = order.price or Decimal("1")
                    approx_qty = current_qty / ref_price
                    adjusted_qty = meta.truncate_amount(approx_qty) if meta else approx_qty
                else:
                    adjusted_qty = meta.truncate_amount(current_qty) if meta else current_qty

                if adjusted_qty <= Decimal("0"):
                    ExecutionStateMachine.transition_leg(
                        leg_state,
                        ExecutionLegStatus.FAILED,
                        error_message="Quantity truncated to zero between sequential hops",
                    )
                    failed_idx = idx
                    break

                # Update order quantity
                order = ExecutionOrder(
                    client_order_id=order.client_order_id,
                    exchange=order.exchange,
                    symbol=order.symbol,
                    side=order.side,
                    order_type=order.order_type,
                    time_in_force=order.time_in_force,
                    price=order.price,
                    quantity=adjusted_qty,
                )
                leg_state.order = order

            # Lock funds for this single leg
            if order.side == OrderSide.BUY:
                lock_asset = quote_asset
                lock_amount = (order.price or Decimal("0")) * order.quantity
            else:
                lock_asset = base_asset
                lock_amount = order.quantity

            locked = await self.balance_manager.lock_funds(order.exchange, lock_asset, lock_amount)
            if not locked:
                ExecutionStateMachine.transition_leg(
                    leg_state,
                    ExecutionLegStatus.FAILED,
                    error_message=f"Could not lock {lock_amount} {lock_asset} on {order.exchange.value}",
                )
                failed_idx = idx
                break

            # Execute leg fill
            await self._simulate_order_fill(leg_state)

            if leg_state.status != ExecutionLegStatus.FILLED:
                failed_idx = idx
                break

            # Output delivered from this leg becomes input to next leg
            if order.side == OrderSide.BUY:
                # Received base asset, net of base fee if fee in base asset
                delivered = leg_state.filled_qty
                if leg_state.fee_asset and leg_state.fee_asset.upper() == base_asset.upper():
                    delivered -= leg_state.fee_paid
                current_qty = delivered
            else:
                # Received quote asset, net of quote fee
                delivered = leg_state.filled_qty * (leg_state.average_fill_price or Decimal("0"))
                if leg_state.fee_asset and leg_state.fee_asset.upper() == quote_asset.upper():
                    delivered -= leg_state.fee_paid
                current_qty = delivered

        if failed_idx == -1:
            self._compute_plan_pnl(plan)
            ExecutionStateMachine.transition_plan(plan, ExecutionPlanStatus.COMPLETED)
        else:
            await self.emergency_unwind(plan, failed_leg_index=failed_idx)

        return plan

    # --------------------------------------------------------------------------
    # Order Fill Simulation
    # --------------------------------------------------------------------------

    async def _simulate_order_fill(self, leg_state: ExecutionLegState) -> ExecutionLegState:
        """Simulate realistic L2 order fill, deducting fees and settling balances."""
        ExecutionStateMachine.transition_leg(leg_state, ExecutionLegStatus.SUBMITTED)

        if self.simulated_latency_ms > 0:
            await asyncio.sleep(self.simulated_latency_ms / 1000.0)

        order = leg_state.order
        base_asset, quote_asset = self._split_symbol(order.symbol)

        # Check L2 depth if orderbook engine is present
        fill_price = order.price or Decimal("0")
        filled_qty = order.quantity

        if self.order_book_engine:
            book = self.order_book_engine.get_book(order.exchange, order.symbol)
            if book:
                if order.side == OrderSide.BUY:
                    vwap, depth_qty = book.vwap_ask(order.quantity)
                else:
                    vwap, depth_qty = book.vwap_bid(order.quantity)

                if depth_qty > Decimal("0") and vwap > Decimal("0"):
                    fill_price = vwap
                    filled_qty = min(order.quantity, depth_qty)

        if fill_price <= Decimal("0") or filled_qty <= Decimal("0"):
            # Release locked funds
            if order.side == OrderSide.BUY:
                await self.balance_manager.release_locked(order.exchange, quote_asset, (order.price or Decimal("0")) * order.quantity)
            else:
                await self.balance_manager.release_locked(order.exchange, base_asset, order.quantity)

            return ExecutionStateMachine.transition_leg(
                leg_state,
                ExecutionLegStatus.FAILED,
                error_message="Order book depth insufficient or zero price",
            )

        # Calculate exact fee
        is_perp = ":" in order.symbol
        inst_type = InstrumentType.LINEAR_PERP if is_perp else InstrumentType.SPOT
        bnb_bal = self.balance_manager.get_balance(order.exchange, "BNB").free

        _, taker_rate = self.fee_calculator.get_fee_rates(
            exchange=order.exchange,
            instrument_type=inst_type,
            enable_bnb_discount=False,
            bnb_balance=bnb_bal,
        )

        notional = filled_qty * fill_price
        fee_in_base = (
            order.exchange == ExchangeId.BINANCE
            and inst_type == InstrumentType.SPOT
            and order.side == OrderSide.BUY
            and bnb_bal < Decimal("0.1")
        )

        if fee_in_base:
            fee_amount = filled_qty * taker_rate
            fee_asset_name = base_asset
        else:
            fee_amount = notional * taker_rate
            fee_asset_name = quote_asset

        # Settle fill in balance manager
        await self.balance_manager.settle_fill(
            exchange=order.exchange,
            base_asset=base_asset,
            quote_asset=quote_asset,
            side=order.side,
            filled_qty=filled_qty,
            fill_price=fill_price,
            fee=fee_amount,
            fee_asset=fee_asset_name,
        )

        leg_state.filled_qty = filled_qty
        leg_state.average_fill_price = fill_price
        leg_state.fee_paid = fee_amount
        leg_state.fee_asset = fee_asset_name

        if filled_qty < order.quantity:
            ExecutionStateMachine.transition_leg(leg_state, ExecutionLegStatus.PARTIALLY_FILLED)
        else:
            ExecutionStateMachine.transition_leg(leg_state, ExecutionLegStatus.FILLED)

        return leg_state

    # --------------------------------------------------------------------------
    # PnL Computation
    # --------------------------------------------------------------------------

    def _compute_plan_pnl(self, plan: ExecutionPlan) -> None:
        """Compute realized net PnL from filled execution legs."""
        if plan.strategy_type in (StrategyType.CROSS_EXCHANGE, StrategyType.CROSS_EXCHANGE_SPOT):
            # Leg 0 = BUY, Leg 1 = SELL (or vice-versa)
            buy_cost = Decimal("0")
            sell_proceeds = Decimal("0")
            for leg in plan.legs:
                price = leg.average_fill_price or Decimal("0")
                notional = leg.filled_qty * price
                fee_usd = leg.fee_paid if (leg.fee_asset == "USDT" or not leg.fee_asset) else (leg.fee_paid * price)
                if leg.order.side == OrderSide.BUY:
                    buy_cost = notional + fee_usd
                else:
                    sell_proceeds = notional - fee_usd
            plan.realized_pnl = sell_proceeds - buy_cost

        elif plan.strategy_type in (StrategyType.TRIANGULAR, StrategyType.TRIANGULAR_SPOT):
            if len(plan.legs) >= 3:
                leg0 = plan.legs[0]
                leg2 = plan.legs[2]
                start_notional = leg0.order.quantity * (leg0.average_fill_price or Decimal("0")) if leg0.order.side == OrderSide.BUY else leg0.order.quantity
                end_notional = leg2.filled_qty * (leg2.average_fill_price or Decimal("0")) if leg2.order.side == OrderSide.SELL else leg2.filled_qty
                plan.realized_pnl = end_notional - start_notional
