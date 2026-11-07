"""Internal event bus schemas for asynchronous event-driven coordination.

Events are immutable messages passed across decoupled system layers
(Collector -> Normalizer -> OrderBook -> Strategy -> Risk -> Executor -> Persistence).
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field

from src.core.models import (
    ArbitrageOpportunity,
    BalanceRecord,
    ExecutionLegState,
    ExecutionOrder,
    ExecutionPlan,
    FundingRateSnapshot,
    HedgedPosition,
    NormalizedOrderBook,
    RiskDecision,
    StatArbSignal,
)


class BaseEvent(BaseModel):
    """Base schema for all internal events."""

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp_ms: int = Field(default_factory=lambda: int(time.time() * 1000))
    event_type: str


class OrderBookUpdateEvent(BaseEvent):
    """Fired when an L2 order book is synchronized and validated."""

    event_type: str = "ORDERBOOK_UPDATE"
    book: NormalizedOrderBook


class OpportunityDetectedEvent(BaseEvent):
    """Fired when a strategy engine identifies an executable arbitrage opportunity."""

    event_type: str = "OPPORTUNITY_DETECTED"
    opportunity: ArbitrageOpportunity


class RiskEvaluatedEvent(BaseEvent):
    """Fired after pre-trade risk evaluation is completed."""

    event_type: str = "RISK_EVALUATED"
    opportunity_id: str
    decision: RiskDecision


class ExecutionRequestedEvent(BaseEvent):
    """Fired to trigger multi-leg execution for an approved opportunity."""

    event_type: str = "EXECUTION_REQUESTED"
    plan: ExecutionPlan


class LegStatusUpdatedEvent(BaseEvent):
    """Fired when an individual leg changes state (submitted, partially filled, filled, failed)."""

    event_type: str = "LEG_STATUS_UPDATED"
    plan_id: str
    leg: ExecutionLegState


class PlanStatusUpdatedEvent(BaseEvent):
    """Fired when an entire execution plan status changes."""

    event_type: str = "PLAN_STATUS_UPDATED"
    plan: ExecutionPlan


class EmergencyUnwindEvent(BaseEvent):
    """Fired when an execution leg fails and triggers automated hedging / flattening."""

    event_type: str = "EMERGENCY_UNWIND"
    plan_id: str
    failed_leg_index: int
    unwind_orders: list[ExecutionOrder]
    reason: str


class BalanceUpdatedEvent(BaseEvent):
    """Fired when account inventory balances are updated from private feed or post-trade fill."""

    event_type: str = "BALANCE_UPDATED"
    balance: BalanceRecord


class SystemHaltEvent(BaseEvent):
    """Fired when circuit breaker or emergency kill switch is engaged."""

    event_type: str = "SYSTEM_HALT"
    reason: str
    source: str


# ==============================================================================
# Derivatives & Stateful Position Events (Strategies 3 & 4)
# ==============================================================================


class FundingRateUpdatedEvent(BaseEvent):
    """Fired when live funding rate or mark price is updated."""

    event_type: str = "FUNDING_RATE_UPDATED"
    snapshot: FundingRateSnapshot


class StatArbSignalEvent(BaseEvent):
    """Fired when Strategy 4 generates an actionable spread signal."""

    event_type: str = "STAT_ARB_SIGNAL"
    signal: StatArbSignal


class PositionHealthEvent(BaseEvent):
    """Fired when an open hedged position requires monitoring or margin rebalancing."""

    event_type: str = "POSITION_HEALTH"
    position: HedgedPosition
    alert_type: str
    message: str
