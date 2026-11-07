"""Formal interfaces and abstract base classes for the Crypto Arbitrage System.

Specialist Agents must implement these contracts without altering their signatures.
Lead Agent retains sole authority over interface modifications.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any, Optional, Protocol, runtime_checkable

from src.core.models import (
    ArbitrageOpportunity,
    BalanceRecord,
    ExchangeId,
    ExecutionLegState,
    ExecutionOrder,
    ExecutionPlan,
    FundingRateSnapshot,
    HedgedPosition,
    NormalizedOrderBook,
    RiskDecision,
    StatArbSignal,
    StrategyType,
    SymbolMetadata,
)


@runtime_checkable
class ExchangeAdapter(Protocol):
    """Protocol for exchange communication (WebSocket feeds and REST endpoints)."""

    @property
    def exchange_id(self) -> ExchangeId:
        """Return the exchange identifier."""
        ...

    async def connect(self) -> None:
        """Establish WebSocket streams and initialize exchange sessions."""
        ...

    async def disconnect(self) -> None:
        """Gracefully disconnect WebSocket connections and clean up resources."""
        ...

    async def subscribe_orderbook(self, symbol: str) -> None:
        """Subscribe to real-time L2 depth stream for a symbol."""
        ...

    async def fetch_orderbook_snapshot(self, symbol: str, limit: int = 1000) -> NormalizedOrderBook:
        """Fetch REST depth snapshot for initial order book hydration."""
        ...

    async def fetch_balances(self) -> dict[str, BalanceRecord]:
        """Fetch current account balances across all registered assets."""
        ...

    async def submit_order(self, order: ExecutionOrder) -> ExecutionLegState:
        """Submit a single trade order (e.g. IOC limit or market) to exchange."""
        ...

    async def cancel_order(self, symbol: str, exchange_order_id: str) -> bool:
        """Cancel an open order on the exchange."""
        ...

    async def get_order_status(self, symbol: str, exchange_order_id: str) -> ExecutionLegState:
        """Query live execution status of an active or completed order."""
        ...

    def get_symbol_metadata(self, symbol: str) -> Optional[SymbolMetadata]:
        """Retrieve cached filter limits and precision rules for a symbol."""
        ...


@runtime_checkable
class OrderBookEngine(Protocol):
    """Protocol for maintaining synchronized and verified local L2 order books."""

    def apply_snapshot(self, snapshot: NormalizedOrderBook) -> None:
        """Hydrate order book state from REST snapshot."""
        ...

    def apply_diff(self, diff_payload: dict[str, Any]) -> NormalizedOrderBook:
        """Apply incremental WebSocket depth update and validate sequence continuity."""
        ...

    def verify_checksum(self, exchange: ExchangeId, symbol: str, expected_checksum: int) -> bool:
        """Verify CRC32 checksum against local top-of-book levels."""
        ...

    def get_book(self, exchange: ExchangeId, symbol: str) -> Optional[NormalizedOrderBook]:
        """Retrieve current in-memory normalized order book."""
        ...

    def is_stale(self, exchange: ExchangeId, symbol: str, max_age_ms: int) -> bool:
        """Check if local order book exceeds freshness threshold or is flagged stale."""
        ...


@runtime_checkable
class StrategyEngine(Protocol):
    """Protocol for quantitative arbitrage detection engines."""

    @property
    def strategy_type(self) -> StrategyType:
        """Return strategy classification."""
        ...

    async def process_orderbook_update(self, book: NormalizedOrderBook) -> list[ArbitrageOpportunity]:
        """Evaluate order book update and return any executable arbitrage opportunities."""
        ...

    def reset(self) -> None:
        """Reset internal cycle graphs or cached state."""
        ...


@runtime_checkable
class RiskManager(Protocol):
    """Protocol for pre-trade risk evaluation, position limits, and balance gating."""

    async def evaluate_opportunity(self, opportunity: ArbitrageOpportunity) -> RiskDecision:
        """Evaluate opportunity against max notional, minimum edge, and active kill switches."""
        ...

    async def check_pre_trade_limits(self, order: ExecutionOrder) -> tuple[bool, Optional[str]]:
        """Validate order size, price deviation, and counterparty exposure limits."""
        ...

    async def check_inventory_availability(
        self, exchange: ExchangeId, asset: str, required_qty: Decimal
    ) -> bool:
        """Verify pre-funded balance availability on target exchange."""
        ...

    async def record_fill(self, leg: ExecutionLegState) -> None:
        """Update internal balance tracking upon leg execution completion."""
        ...


@runtime_checkable
class Executor(Protocol):
    """Protocol for orchestrating multi-leg execution and automated unwinding."""

    async def execute_plan(self, plan: ExecutionPlan) -> ExecutionPlan:
        """Orchestrate concurrent or sequential execution of all legs in plan."""
        ...

    async def emergency_unwind(self, plan: ExecutionPlan, failed_leg_index: int) -> bool:
        """Execute automated market hedge to neutralize directional exposure from partial fill."""
        ...

    async def cancel_all_open_orders(self) -> None:
        """Emergency circuit breaker: Cancel all in-flight orders across all exchanges."""
        ...


@runtime_checkable
class AsyncPersistenceWriter(Protocol):
    """Protocol for non-blocking asynchronous dual-queue persistence engine.

    Maintains two separate asynchronous queues:
    1. critical_queue (unbounded): Execution plans, leg states, risk events, executable opportunities.
    2. telemetry_queue (bounded, maxsize=10000): Raw L2 snapshots and theoretical opportunities dropped
       via put_nowait when full to prevent event loop backpressure.
    """

    async def enqueue_critical(self, record: Any) -> None:
        """Enqueue critical transaction records into unbounded non-dropping queue."""
        ...

    async def enqueue_telemetry(self, record: Any) -> None:
        """Enqueue high-frequency analytical snapshots into bounded droppable queue."""
        ...

    async def enqueue_opportunity(self, opportunity: ArbitrageOpportunity) -> None:
        """Enqueue opportunity (executable -> critical, theoretical -> telemetry)."""
        ...

    async def enqueue_execution_plan(self, plan: ExecutionPlan) -> None:
        """Enqueue execution plan and leg states (critical)."""
        ...

    async def enqueue_order_book_snapshot(self, book: NormalizedOrderBook) -> None:
        """Enqueue order book snapshot for analytical auditing (telemetry)."""
        ...

    async def enqueue_balance(self, balance: BalanceRecord) -> None:
        """Enqueue account balance snapshot (critical)."""
        ...

    async def enqueue_position(self, position: HedgedPosition) -> None:
        """Enqueue stateful hedged position record (critical)."""
        ...

    async def enqueue_funding_snapshot(self, snapshot: FundingRateSnapshot) -> None:
        """Enqueue funding rate & basis snapshot (telemetry)."""
        ...

    async def flush(self) -> None:
        """Wait for pending write queues to drain to storage."""
        ...

    async def start(self) -> None:
        """Start background worker task."""
        ...

    async def stop(self) -> None:
        """Stop background worker task after draining."""
        ...


@runtime_checkable
class DerivativesExchangeAdapter(ExchangeAdapter, Protocol):
    """Protocol for derivatives exchanges supporting Perpetual Futures and funding streams."""

    async def subscribe_funding_and_mark_price(self, symbols: list[str]) -> None:
        """Subscribe to live mark price and funding rate streaming feeds."""
        ...

    async def fetch_funding_rate_history(
        self, symbol: str, limit: int = 100
    ) -> list[FundingRateSnapshot]:
        """Fetch historical funding rate records for basis analysis."""
        ...

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set position leverage for perpetual contract (governed by RiskManager)."""
        ...

    async def fetch_positions(self) -> list[HedgedPosition]:
        """Fetch active perpetual positions and margin state."""
        ...

    async def fetch_margin_health(self) -> dict[str, Any]:
        """Query account margin balance, maintenance margin, and margin ratio."""
        ...


@runtime_checkable
class PositionManager(Protocol):
    """Protocol for lifecycle management of stateful multi-leg positions (Strategies 3 & 4)."""

    async def open_hedged_position(self, plan: ExecutionPlan) -> HedgedPosition:
        """Register and track a newly executed hedged position."""
        ...

    async def update_position_marks(self, marks: dict[str, Decimal]) -> list[HedgedPosition]:
        """Mark-to-market all open positions, recomputing unrealized PnL and delta drift."""
        ...

    async def accrue_funding(self, event: Any) -> None:
        """Accrue funding payout or fee to open hedged positions."""
        ...

    async def close_hedged_position(self, position_id: str, reason: str) -> bool:
        """Trigger multi-leg exit orders to flatten open hedged position."""
        ...

    def get_open_positions(self) -> list[HedgedPosition]:
        """Return all active unclosed hedged positions."""
        ...


@runtime_checkable
class TimeSeriesBuffer(Protocol):
    """Protocol for pre-allocated ring buffer storing mid-prices and basis history."""

    def append(self, timestamp_ms: int, val_a: Decimal, val_b: Optional[Decimal] = None) -> None:
        """Append price ticks to ring buffer (zero Pandas on hot path)."""
        ...

    def get_spread_series(
        self, beta: Decimal
    ) -> tuple[list[Decimal], Decimal, Decimal, Decimal]:
        """Compute rolling spread series S_t = ln(P_a) - beta * ln(P_b), mean, and std."""
        ...

    def calculate_z_score(
        self, beta: Decimal, current_spread: Decimal
    ) -> tuple[Decimal, Decimal, Decimal]:
        """Compute Z-score: (current_spread - rolling_mean) / rolling_std."""
        ...

    def estimate_half_life(self, beta: Decimal) -> Optional[Decimal]:
        """Estimate Ornstein-Uhlenbeck mean-reversion half-life."""
        ...
