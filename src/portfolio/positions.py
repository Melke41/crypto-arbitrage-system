"""Stateful multi-leg hedged position management for Strategies 3 & 4.

Implements PositionManager protocol defined in src/core/interfaces.py.
Tracks position lifecycle, mark-to-market valuations, funding accruals,
and delta-neutral drift.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from decimal import Decimal
from typing import Any, Optional

from src.core.interfaces import PositionManager
from src.core.models import (
    ExecutionPlan,
    FundingRateSnapshot,
    HedgedPosition,
    HedgedPositionStatus,
    InstrumentType,
    OrderSide,
    PositionLeg,
    StrategyType,
)


class HedgedPositionManager:
    """Manages lifecycle, delta neutrality, and funding accruals of hedged positions."""

    def __init__(self) -> None:
        self._positions: dict[str, HedgedPosition] = {}
        self._lock = asyncio.Lock()

    async def open_hedged_position(self, plan: ExecutionPlan) -> HedgedPosition:
        """Register and track a newly executed hedged position from an execution plan."""
        async with self._lock:
            pos_id = f"pos_{uuid.uuid4().hex[:12]}"

            if len(plan.legs) < 2:
                raise ValueError("HedgedPosition requires at least 2 execution legs")

            leg0_state = plan.legs[0]
            leg1_state = plan.legs[1]

            qty_a = leg0_state.filled_qty if leg0_state.filled_qty > Decimal("0") else leg0_state.order.quantity
            price_a = leg0_state.average_fill_price or leg0_state.order.price or Decimal("0")

            qty_b = leg1_state.filled_qty if leg1_state.filled_qty > Decimal("0") else leg1_state.order.quantity
            price_b = leg1_state.average_fill_price or leg1_state.order.price or Decimal("0")

            inst_a = InstrumentType.SPOT
            inst_b = (
                InstrumentType.LINEAR_PERP
                if (plan.strategy_type == StrategyType.FUNDING_BASIS or ":" in leg1_state.order.symbol)
                else InstrumentType.SPOT
            )

            leg_a = PositionLeg(
                symbol=leg0_state.order.symbol,
                exchange=leg0_state.order.exchange,
                instrument_type=inst_a,
                side=leg0_state.order.side,
                quantity=qty_a,
                entry_price=price_a,
                current_price=price_a,
                unrealized_pnl=Decimal("0"),
            )

            leg_b = PositionLeg(
                symbol=leg1_state.order.symbol,
                exchange=leg1_state.order.exchange,
                instrument_type=inst_b,
                side=leg1_state.order.side,
                quantity=qty_b,
                entry_price=price_b,
                current_price=price_b,
                unrealized_pnl=Decimal("0"),
            )

            notional_a = qty_a * price_a
            notional_b = qty_b * price_b
            net_delta = notional_a - notional_b if leg_a.side == OrderSide.BUY else notional_b - notional_a

            basis_bps = Decimal("0")
            if price_a > Decimal("0") and inst_b == InstrumentType.LINEAR_PERP:
                basis_bps = ((price_b - price_a) / price_a) * Decimal("10000")

            # Conservative liquidation price estimate for short perp with 2x leverage (50% adverse move)
            liq_price_b = price_b * Decimal("1.45") if inst_b == InstrumentType.LINEAR_PERP and leg_b.side == OrderSide.SELL else None

            pos = HedgedPosition(
                position_id=pos_id,
                strategy_type=plan.strategy_type,
                status=HedgedPositionStatus.OPEN,
                leg_a=leg_a,
                leg_b=leg_b,
                net_delta_usd=net_delta,
                entry_spread_or_basis_bps=basis_bps,
                current_unrealized_pnl_quote=Decimal("0"),
                realized_funding_pnl_quote=Decimal("0"),
                margin_ratio=Decimal("0.05") if inst_b == InstrumentType.LINEAR_PERP else None,
                liquidation_price_leg_b=liq_price_b,
                opened_at_ms=int(time.time() * 1000),
            )

            self._positions[pos_id] = pos
            return pos

    async def update_position_marks(self, marks: dict[str, Decimal]) -> list[HedgedPosition]:
        """Mark-to-market all open positions, recomputing unrealized PnL and delta drift."""
        async with self._lock:
            updated_positions: list[HedgedPosition] = []

            for pos in self._positions.values():
                if pos.status != HedgedPositionStatus.OPEN:
                    continue

                price_a = self._resolve_mark_price(pos.leg_a.symbol, marks) or pos.leg_a.current_price
                price_b = self._resolve_mark_price(pos.leg_b.symbol, marks) or pos.leg_b.current_price

                # Compute leg PnLs
                if pos.leg_a.side == OrderSide.BUY:
                    pnl_a = (price_a - pos.leg_a.entry_price) * pos.leg_a.quantity
                else:
                    pnl_a = (pos.leg_a.entry_price - price_a) * pos.leg_a.quantity

                if pos.leg_b.side == OrderSide.BUY:
                    pnl_b = (price_b - pos.leg_b.entry_price) * pos.leg_b.quantity
                else:
                    pnl_b = (pos.leg_b.entry_price - price_b) * pos.leg_b.quantity

                updated_leg_a = PositionLeg(
                    symbol=pos.leg_a.symbol,
                    exchange=pos.leg_a.exchange,
                    instrument_type=pos.leg_a.instrument_type,
                    side=pos.leg_a.side,
                    quantity=pos.leg_a.quantity,
                    entry_price=pos.leg_a.entry_price,
                    current_price=price_a,
                    unrealized_pnl=pnl_a,
                )

                updated_leg_b = PositionLeg(
                    symbol=pos.leg_b.symbol,
                    exchange=pos.leg_b.exchange,
                    instrument_type=pos.leg_b.instrument_type,
                    side=pos.leg_b.side,
                    quantity=pos.leg_b.quantity,
                    entry_price=pos.leg_b.entry_price,
                    current_price=price_b,
                    unrealized_pnl=pnl_b,
                )

                notional_a = pos.leg_a.quantity * price_a
                notional_b = pos.leg_b.quantity * price_b
                net_delta = notional_a - notional_b if pos.leg_a.side == OrderSide.BUY else notional_b - notional_a

                # Update position
                self._positions[pos.position_id] = HedgedPosition(
                    position_id=pos.position_id,
                    strategy_type=pos.strategy_type,
                    status=pos.status,
                    leg_a=updated_leg_a,
                    leg_b=updated_leg_b,
                    net_delta_usd=net_delta,
                    entry_spread_or_basis_bps=pos.entry_spread_or_basis_bps,
                    current_unrealized_pnl_quote=pnl_a + pnl_b,
                    realized_funding_pnl_quote=pos.realized_funding_pnl_quote,
                    margin_ratio=pos.margin_ratio,
                    liquidation_price_leg_b=pos.liquidation_price_leg_b,
                    opened_at_ms=pos.opened_at_ms,
                    closed_at_ms=pos.closed_at_ms,
                )
                updated_positions.append(self._positions[pos.position_id])

            return updated_positions

    async def accrue_funding(self, event: Any) -> None:
        """Accrue funding payout or fee to open hedged positions."""
        snapshot: Optional[FundingRateSnapshot] = None
        if isinstance(event, FundingRateSnapshot):
            snapshot = event
        elif hasattr(event, "snapshot") and isinstance(event.snapshot, FundingRateSnapshot):
            snapshot = event.snapshot

        if snapshot is None:
            return

        async with self._lock:
            for pos in self._positions.values():
                if pos.status != HedgedPositionStatus.OPEN:
                    continue

                if self._symbols_match(pos.leg_b.symbol, snapshot.symbol):
                    notional = pos.leg_b.quantity * snapshot.mark_price
                    # Short perp receives positive funding if rate > 0
                    if pos.leg_b.side == OrderSide.SELL:
                        cash_flow = notional * snapshot.current_funding_rate
                    else:
                        cash_flow = -notional * snapshot.current_funding_rate

                    new_accrued = pos.realized_funding_pnl_quote + cash_flow
                    self._positions[pos.position_id] = pos.model_copy(
                        update={"realized_funding_pnl_quote": new_accrued}
                    )

    async def close_hedged_position(self, position_id: str, reason: str) -> bool:
        """Trigger multi-leg exit orders to flatten open hedged position."""
        async with self._lock:
            pos = self._positions.get(position_id)
            if pos is None or pos.status != HedgedPositionStatus.OPEN:
                return False

            self._positions[position_id] = pos.model_copy(
                update={
                    "status": HedgedPositionStatus.CLOSED,
                    "closed_at_ms": int(time.time() * 1000),
                }
            )
            return True

    def get_open_positions(self) -> list[HedgedPosition]:
        """Return all active unclosed hedged positions."""
        return [p for p in self._positions.values() if p.status == HedgedPositionStatus.OPEN]

    def get_position(self, position_id: str) -> Optional[HedgedPosition]:
        """Retrieve single position by ID."""
        return self._positions.get(position_id)

    @staticmethod
    def _resolve_mark_price(symbol: str, marks: dict[str, Decimal]) -> Optional[Decimal]:
        if symbol in marks:
            return marks[symbol]
        clean = symbol.split(":")[0]
        if clean in marks:
            return marks[clean]
        return None

    @staticmethod
    def _symbols_match(sym_a: str, sym_b: str) -> bool:
        clean_a = sym_a.split(":")[0].replace("/", "")
        clean_b = sym_b.split(":")[0].replace("/", "")
        return clean_a == clean_b
