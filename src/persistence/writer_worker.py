"""Non-blocking dual-queue asynchronous persistence writer.

Implements AsyncPersistenceWriter protocol from src/core/interfaces.py.
Maintains two decoupled queues:
1. critical_queue (unbounded): never drops critical execution plans, legs, or positions.
2. telemetry_queue (bounded, maxsize=10000): drops high-frequency snapshots via put_nowait
   when full to eliminate event-loop backpressure.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.interfaces import AsyncPersistenceWriter
from src.core.models import (
    ArbitrageOpportunity,
    BalanceRecord,
    ExecutionPlan,
    FundingRateSnapshot,
    HedgedPosition,
    NormalizedOrderBook,
    OpportunityLifecycleState,
)
from src.persistence.repositories import ArbitrageRepository

logger = logging.getLogger(__name__)


class DualQueuePersistenceWriter(AsyncPersistenceWriter):
    """Asynchronous dual-queue writer buffering trading events into persistent storage."""

    def __init__(
        self,
        session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
        telemetry_queue_maxsize: int = 10000,
        batch_size: int = 50,
        flush_interval_sec: float = 0.5,
    ) -> None:
        self.session_factory = session_factory
        self.critical_queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=0)
        self.telemetry_queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=telemetry_queue_maxsize)
        self.batch_size = batch_size
        self.flush_interval_sec = flush_interval_sec

        self.dropped_telemetry_count: int = 0
        self.persisted_critical_count: int = 0
        self.persisted_telemetry_count: int = 0

        self._worker_task: Optional[asyncio.Task[None]] = None
        self._running: bool = False
        self._lock = asyncio.Lock()

    # --------------------------------------------------------------------------
    # Queue Ingestion
    # --------------------------------------------------------------------------

    async def enqueue_critical(self, record: Any) -> None:
        """Enqueue critical transactional record into unbounded non-dropping queue."""
        await self.critical_queue.put(record)

    async def enqueue_telemetry(self, record: Any) -> None:
        """Enqueue analytical snapshot into bounded queue, dropping gracefully on overflow."""
        try:
            self.telemetry_queue.put_nowait(record)
        except asyncio.QueueFull:
            self.dropped_telemetry_count += 1

    async def enqueue_opportunity(self, opportunity: ArbitrageOpportunity) -> None:
        """Route opportunity to critical or telemetry queue based on lifecycle state."""
        if opportunity.lifecycle_state in (
            OpportunityLifecycleState.EXECUTABLE_OPPORTUNITY,
            OpportunityLifecycleState.SIMULATED_FILL,
            OpportunityLifecycleState.DEMO_TESTNET_FILL,
            OpportunityLifecycleState.LIVE_FILL,
        ):
            await self.enqueue_critical(opportunity)
        else:
            await self.enqueue_telemetry(opportunity)

    async def enqueue_execution_plan(self, plan: ExecutionPlan) -> None:
        """Enqueue execution plan and leg states (critical)."""
        await self.enqueue_critical(plan)

    async def enqueue_order_book_snapshot(self, book: NormalizedOrderBook) -> None:
        """Enqueue order book snapshot for analytical auditing (telemetry)."""
        await self.enqueue_telemetry(book)

    async def enqueue_balance(self, balance: BalanceRecord) -> None:
        """Enqueue account balance snapshot (critical)."""
        await self.enqueue_critical(balance)

    async def enqueue_position(self, position: HedgedPosition) -> None:
        """Enqueue stateful hedged position record (critical)."""
        await self.enqueue_critical(position)

    async def enqueue_funding_snapshot(self, snapshot: FundingRateSnapshot) -> None:
        """Enqueue funding rate & basis snapshot (telemetry)."""
        await self.enqueue_telemetry(snapshot)

    # --------------------------------------------------------------------------
    # Lifecycle & Worker Management
    # --------------------------------------------------------------------------

    async def start(self) -> None:
        """Start background drain worker."""
        if self._running:
            return
        self._running = True
        self._worker_task = asyncio.create_task(self._drain_loop())

    async def stop(self) -> None:
        """Stop background worker after draining active queues."""
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None
        await self.flush()

    async def flush(self) -> None:
        """Drain all pending items in critical and telemetry queues to storage."""
        if not self.session_factory:
            # Drain in-memory queues without DB
            while not self.critical_queue.empty():
                self.critical_queue.get_nowait()
                self.critical_queue.task_done()
                self.persisted_critical_count += 1
            while not self.telemetry_queue.empty():
                self.telemetry_queue.get_nowait()
                self.telemetry_queue.task_done()
                self.persisted_telemetry_count += 1
            return

        async with self._lock:
            async with self.session_factory() as session:
                # Flush critical queue
                while not self.critical_queue.empty():
                    item = self.critical_queue.get_nowait()
                    await self._persist_item(session, item)
                    self.critical_queue.task_done()
                    self.persisted_critical_count += 1

                # Flush telemetry queue
                while not self.telemetry_queue.empty():
                    item = self.telemetry_queue.get_nowait()
                    await self._persist_item(session, item)
                    self.telemetry_queue.task_done()
                    self.persisted_telemetry_count += 1

                await session.commit()

    async def _drain_loop(self) -> None:
        """Continuous background worker draining queues periodically."""
        while self._running:
            try:
                await asyncio.sleep(self.flush_interval_sec)
                await self.flush()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in persistence drain worker: {e}", exc_info=True)

    async def _persist_item(self, session: AsyncSession, item: Any) -> None:
        """Map entity to repository save method."""
        if isinstance(item, ArbitrageOpportunity):
            await ArbitrageRepository.save_opportunity(session, item)
        elif isinstance(item, ExecutionPlan):
            await ArbitrageRepository.save_execution_plan(session, item)
        elif isinstance(item, HedgedPosition):
            await ArbitrageRepository.save_position(session, item)
        elif isinstance(item, BalanceRecord):
            await ArbitrageRepository.save_balance(session, item)
        elif isinstance(item, NormalizedOrderBook):
            await ArbitrageRepository.save_orderbook_snapshot(session, item)
