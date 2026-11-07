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
    NormalizedOrderBook,
    RiskDecision,
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
    """Protocol for non-blocking asynchronous persistence queue."""

    async def enqueue_opportunity(self, opportunity: ArbitrageOpportunity) -> None:
        """Enqueue opportunity for background database persistence."""
        ...

    async def enqueue_execution_plan(self, plan: ExecutionPlan) -> None:
        """Enqueue execution plan and leg states for background database persistence."""
        ...

    async def enqueue_order_book_snapshot(self, book: NormalizedOrderBook) -> None:
        """Enqueue order book snapshot for analytical auditing."""
        ...

    async def enqueue_balance(self, balance: BalanceRecord) -> None:
        """Enqueue account balance snapshot."""
        ...

    async def flush(self) -> None:
        """Wait for pending write queue to drain to storage."""
        ...

    async def start(self) -> None:
        """Start background worker task."""
        ...

    async def stop(self) -> None:
        """Stop background worker task after draining."""
        ...
