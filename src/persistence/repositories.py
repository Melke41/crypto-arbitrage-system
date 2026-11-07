"""Async repository access patterns for arbitrage data entities."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Optional
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models import (
    ArbitrageOpportunity,
    BalanceRecord,
    ExecutionLegState,
    ExecutionPlan,
    HedgedPosition,
    NormalizedOrderBook,
)
from src.persistence.models import (
    BalanceSnapshotRecord,
    ExecutionLegRecord,
    ExecutionPlanRecord,
    HedgedPositionRecord,
    OpportunityRecord,
    OrderBookSnapshotRecord,
    RiskEventRecord,
)


class ArbitrageRepository:
    """Asynchronous CRUD methods for persisting and querying arbitrage data."""

    @staticmethod
    async def save_opportunity(session: AsyncSession, opp: ArbitrageOpportunity) -> OpportunityRecord:
        """Persist or update an ArbitrageOpportunity."""
        record = await session.get(OpportunityRecord, opp.id)
        if record is None:
            record = OpportunityRecord(
                id=opp.id,
                strategy_type=opp.strategy_type.value,
                lifecycle_state=opp.lifecycle_state.value,
                symbol_legs=json.dumps(opp.symbol_legs),
                gross_edge_bps=opp.gross_edge_bps,
                net_edge_bps=opp.net_edge_bps,
                target_notional=opp.target_notional,
                expected_net_profit=opp.expected_net_profit,
                profit_asset=opp.profit_asset,
                timestamp_ms=opp.timestamp_ms,
                metadata_json=json.dumps({k: str(v) for k, v in opp.metadata.items()}),
            )
            session.add(record)
        else:
            record.lifecycle_state = opp.lifecycle_state.value
            record.net_edge_bps = opp.net_edge_bps
            record.expected_net_profit = opp.expected_net_profit
        await session.commit()
        return record

    @staticmethod
    async def save_execution_plan(session: AsyncSession, plan: ExecutionPlan) -> ExecutionPlanRecord:
        """Persist or update an ExecutionPlan and all associated ExecutionLegs."""
        record = await session.get(ExecutionPlanRecord, plan.plan_id)
        if record is None:
            record = ExecutionPlanRecord(
                plan_id=plan.plan_id,
                opportunity_id=plan.opportunity_id,
                strategy_type=plan.strategy_type.value,
                status=plan.status.value,
                target_notional=plan.target_notional,
                realized_pnl=plan.realized_pnl,
                pnl_asset=plan.pnl_asset,
                created_at_ms=plan.created_at_ms,
                updated_at_ms=plan.updated_at_ms,
            )
            session.add(record)
            for leg in plan.legs:
                leg_rec = ExecutionLegRecord(
                    plan_id=plan.plan_id,
                    leg_index=leg.leg_index,
                    exchange=leg.order.exchange.value,
                    symbol=leg.order.symbol,
                    side=leg.order.side.value,
                    order_type=leg.order.order_type.value,
                    status=leg.status.value,
                    price=leg.order.price,
                    quantity=leg.order.quantity,
                    filled_qty=leg.filled_qty,
                    average_fill_price=leg.average_fill_price,
                    fee_paid=leg.fee_paid,
                    fee_asset=leg.fee_asset,
                    exchange_order_id=leg.exchange_order_id,
                    error_message=leg.error_message,
                )
                session.add(leg_rec)
        else:
            record.status = plan.status.value
            record.realized_pnl = plan.realized_pnl
            record.updated_at_ms = plan.updated_at_ms
            # Update existing leg records
            stmt = select(ExecutionLegRecord).where(ExecutionLegRecord.plan_id == plan.plan_id)
            result = await session.execute(stmt)
            existing_legs = {l.leg_index: l for l in result.scalars().all()}
            for leg in plan.legs:
                if leg.leg_index in existing_legs:
                    l_rec = existing_legs[leg.leg_index]
                    l_rec.status = leg.status.value
                    l_rec.filled_qty = leg.filled_qty
                    l_rec.average_fill_price = leg.average_fill_price
                    l_rec.fee_paid = leg.fee_paid
                    l_rec.fee_asset = leg.fee_asset
                    l_rec.error_message = leg.error_message
                    l_rec.exchange_order_id = leg.exchange_order_id

        await session.commit()
        return record

    @staticmethod
    async def save_position(session: AsyncSession, pos: HedgedPosition) -> HedgedPositionRecord:
        """Persist or update a stateful HedgedPosition."""
        record = await session.get(HedgedPositionRecord, pos.position_id)
        if record is None:
            record = HedgedPositionRecord(
                position_id=pos.position_id,
                strategy_type=pos.strategy_type.value,
                status=pos.status.value,
                leg_a_json=pos.leg_a.model_dump_json(),
                leg_b_json=pos.leg_b.model_dump_json(),
                net_delta_usd=pos.net_delta_usd,
                entry_spread_or_basis_bps=pos.entry_spread_or_basis_bps,
                current_unrealized_pnl_quote=pos.current_unrealized_pnl_quote,
                realized_funding_pnl_quote=pos.realized_funding_pnl_quote,
                margin_ratio=pos.margin_ratio,
                liquidation_price_leg_b=pos.liquidation_price_leg_b,
                opened_at_ms=pos.opened_at_ms,
                closed_at_ms=pos.closed_at_ms,
            )
            session.add(record)
        else:
            record.status = pos.status.value
            record.leg_a_json = pos.leg_a.model_dump_json()
            record.leg_b_json = pos.leg_b.model_dump_json()
            record.net_delta_usd = pos.net_delta_usd
            record.current_unrealized_pnl_quote = pos.current_unrealized_pnl_quote
            record.realized_funding_pnl_quote = pos.realized_funding_pnl_quote
            record.margin_ratio = pos.margin_ratio
            record.closed_at_ms = pos.closed_at_ms
        await session.commit()
        return record

    @staticmethod
    async def save_balance(session: AsyncSession, bal: BalanceRecord) -> BalanceSnapshotRecord:
        """Record account balance snapshot."""
        record = BalanceSnapshotRecord(
            exchange=bal.exchange.value,
            asset=bal.asset,
            free=bal.free,
            locked=bal.locked,
            total=bal.total,
            updated_at_ms=bal.updated_at_ms,
        )
        session.add(record)
        await session.commit()
        return record

    @staticmethod
    async def save_orderbook_snapshot(session: AsyncSession, book: NormalizedOrderBook) -> OrderBookSnapshotRecord:
        """Record L2 depth snapshot telemetry."""
        record = OrderBookSnapshotRecord(
            exchange=book.exchange.value,
            symbol=book.symbol,
            best_bid=book.best_bid.price if book.best_bid else None,
            best_ask=book.best_ask.price if book.best_ask else None,
            spread_bps=book.spread_bps,
            sequence_id=book.sequence_id,
            timestamp_ms=book.local_receipt_timestamp_ms,
        )
        session.add(record)
        await session.commit()
        return record

    @staticmethod
    async def save_risk_event(
        session: AsyncSession,
        event_type: str,
        reason: str,
        source: str,
        details_json: Optional[str] = None,
        timestamp_ms: Optional[int] = None,
    ) -> RiskEventRecord:
        """Record risk event or kill-switch trip."""
        import time
        now_ms = timestamp_ms or int(time.time() * 1000)
        record = RiskEventRecord(
            event_type=event_type,
            reason=reason,
            source=source,
            details_json=details_json,
            timestamp_ms=now_ms,
        )
        session.add(record)
        await session.commit()
        return record

    @staticmethod
    async def get_opportunity(session: AsyncSession, opp_id: str) -> Optional[OpportunityRecord]:
        return await session.get(OpportunityRecord, opp_id)

    @staticmethod
    async def get_execution_plan(session: AsyncSession, plan_id: str) -> Optional[ExecutionPlanRecord]:
        stmt = (
            select(ExecutionPlanRecord)
            .where(ExecutionPlanRecord.plan_id == plan_id)
            .options(selectinload(ExecutionPlanRecord.legs))
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    async def get_hedged_position(session: AsyncSession, pos_id: str) -> Optional[HedgedPositionRecord]:
        return await session.get(HedgedPositionRecord, pos_id)
