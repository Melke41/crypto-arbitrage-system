"""SQLAlchemy 2.0 async ORM models for database persistence."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional
from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """SQLAlchemy 2.0 declarative base."""
    pass


class OpportunityRecord(Base):
    """Persisted arbitrage opportunity record."""

    __tablename__ = "arbitrage_opportunities"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    strategy_type: Mapped[str] = mapped_column(String(32), index=True)
    lifecycle_state: Mapped[str] = mapped_column(String(32), index=True)
    symbol_legs: Mapped[str] = mapped_column(Text)
    gross_edge_bps: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    net_edge_bps: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    target_notional: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    expected_net_profit: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    profit_asset: Mapped[str] = mapped_column(String(16), default="USDT")
    timestamp_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class ExecutionPlanRecord(Base):
    """Persisted multi-leg execution plan."""

    __tablename__ = "execution_plans"

    plan_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    opportunity_id: Mapped[str] = mapped_column(String(64), index=True)
    strategy_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), index=True)
    target_notional: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal("0"))
    pnl_asset: Mapped[str] = mapped_column(String(16), default="USDT")
    created_at_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    updated_at_ms: Mapped[int] = mapped_column(BigInteger)

    legs: Mapped[list[ExecutionLegRecord]] = relationship(
        "ExecutionLegRecord", back_populates="plan", cascade="all, delete-orphan", lazy="selectin"
    )


class ExecutionLegRecord(Base):
    """Persisted individual execution leg within an execution plan."""

    __tablename__ = "execution_legs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[str] = mapped_column(String(64), ForeignKey("execution_plans.plan_id"), index=True)
    leg_index: Mapped[int] = mapped_column(Integer)
    exchange: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(16))
    order_type: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32))
    price: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 8), nullable=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    filled_qty: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal("0"))
    average_fill_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 8), nullable=True)
    fee_paid: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal("0"))
    fee_asset: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    exchange_order_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    plan: Mapped[ExecutionPlanRecord] = relationship("ExecutionPlanRecord", back_populates="legs")


class HedgedPositionRecord(Base):
    """Persisted stateful hedged position for Strategies 3 & 4."""

    __tablename__ = "hedged_positions"

    position_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    strategy_type: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    leg_a_json: Mapped[str] = mapped_column(Text)
    leg_b_json: Mapped[str] = mapped_column(Text)
    net_delta_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal("0"))
    entry_spread_or_basis_bps: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal("0"))
    current_unrealized_pnl_quote: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal("0"))
    realized_funding_pnl_quote: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal("0"))
    margin_ratio: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 8), nullable=True)
    liquidation_price_leg_b: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 8), nullable=True)
    opened_at_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    closed_at_ms: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)


class BalanceSnapshotRecord(Base):
    """Persisted account inventory snapshot."""

    __tablename__ = "balance_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String(32), index=True)
    asset: Mapped[str] = mapped_column(String(16), index=True)
    free: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    locked: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    total: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    updated_at_ms: Mapped[int] = mapped_column(BigInteger, index=True)


class OrderBookSnapshotRecord(Base):
    """Persisted L2 depth telemetry snapshot."""

    __tablename__ = "order_book_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    best_bid: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 8), nullable=True)
    best_ask: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 8), nullable=True)
    spread_bps: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 8), nullable=True)
    sequence_id: Mapped[int] = mapped_column(BigInteger, default=0)
    timestamp_ms: Mapped[int] = mapped_column(BigInteger, index=True)


class RiskEventRecord(Base):
    """Persisted risk threshold or kill switch event."""

    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(64))
    details_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    timestamp_ms: Mapped[int] = mapped_column(BigInteger, index=True)
