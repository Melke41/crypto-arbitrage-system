"""Unit tests for asynchronous database persistence and dual-queue buffer."""

import time
from decimal import Decimal
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models import (
    ArbitrageLeg,
    ArbitrageOpportunity,
    BalanceRecord,
    ExchangeId,
    ExecutionLegState,
    ExecutionOrder,
    ExecutionPlan,
    ExecutionPlanStatus,
    HedgedPosition,
    HedgedPositionStatus,
    InstrumentType,
    NormalizedOrderBook,
    OpportunityLifecycleState,
    OrderBookLevel,
    OrderSide,
    PositionLeg,
    StrategyType,
)
from src.persistence.database import create_db_engine, drop_db, get_session_factory, init_db
from src.persistence.repositories import ArbitrageRepository
from src.persistence.writer_worker import DualQueuePersistenceWriter


@pytest.fixture
async def db_session_factory():
    engine = create_db_engine("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    session_factory = get_session_factory(engine)
    yield session_factory
    await drop_db(engine)
    await engine.dispose()


@pytest.mark.asyncio
async def test_repository_save_and_retrieve_opportunity(db_session_factory):
    async with db_session_factory() as session:
        now_ms = int(time.time() * 1000)
        opp = ArbitrageOpportunity(
            id="opp_db_1",
            strategy_type=StrategyType.CROSS_EXCHANGE,
            lifecycle_state=OpportunityLifecycleState.EXECUTABLE_OPPORTUNITY,
            symbol_legs=["BTC/USDT", "BTC/USDT"],
            legs=[],
            gross_edge_bps=Decimal("35.0"),
            net_edge_bps=Decimal("18.5"),
            target_notional=Decimal("1000.0"),
            expected_net_profit=Decimal("1.85"),
            profit_asset="USDT",
            timestamp_ms=now_ms,
        )

        saved = await ArbitrageRepository.save_opportunity(session, opp)
        assert saved.id == "opp_db_1"

        retrieved = await ArbitrageRepository.get_opportunity(session, "opp_db_1")
        assert retrieved is not None
        assert retrieved.id == "opp_db_1"
        assert retrieved.net_edge_bps == Decimal("18.5")
        assert retrieved.strategy_type == "CROSS_EXCHANGE"


@pytest.mark.asyncio
async def test_repository_save_and_retrieve_execution_plan(db_session_factory):
    async with db_session_factory() as session:
        leg0 = ExecutionLegState(
            leg_index=0,
            order=ExecutionOrder(
                client_order_id="leg0",
                exchange=ExchangeId.BINANCE,
                symbol="BTC/USDT",
                side=OrderSide.BUY,
                quantity=Decimal("0.02"),
                price=Decimal("50000.0"),
            ),
            filled_qty=Decimal("0.02"),
            average_fill_price=Decimal("50000.0"),
            fee_paid=Decimal("1.0"),
            fee_asset="USDT",
        )
        plan = ExecutionPlan(
            plan_id="plan_db_1",
            opportunity_id="opp_db_1",
            strategy_type=StrategyType.CROSS_EXCHANGE,
            status=ExecutionPlanStatus.COMPLETED,
            legs=[leg0],
            target_notional=Decimal("1000.0"),
            realized_pnl=Decimal("2.5"),
        )

        saved = await ArbitrageRepository.save_execution_plan(session, plan)
        assert saved.plan_id == "plan_db_1"

        retrieved = await ArbitrageRepository.get_execution_plan(session, "plan_db_1")
        assert retrieved is not None
        assert retrieved.plan_id == "plan_db_1"
        assert retrieved.status == "COMPLETED"
        assert len(retrieved.legs) == 1
        assert retrieved.legs[0].filled_qty == Decimal("0.02")


@pytest.mark.asyncio
async def test_repository_save_and_retrieve_hedged_position(db_session_factory):
    async with db_session_factory() as session:
        leg_a = PositionLeg(
            symbol="BTC/USDT",
            exchange=ExchangeId.BINANCE,
            instrument_type=InstrumentType.SPOT,
            side=OrderSide.BUY,
            quantity=Decimal("1.0"),
            entry_price=Decimal("50000.0"),
            current_price=Decimal("50500.0"),
            unrealized_pnl=Decimal("500.0"),
        )
        leg_b = PositionLeg(
            symbol="BTC/USDT:USDT",
            exchange=ExchangeId.BINANCE,
            instrument_type=InstrumentType.LINEAR_PERP,
            side=OrderSide.SELL,
            quantity=Decimal("1.0"),
            entry_price=Decimal("50100.0"),
            current_price=Decimal("50550.0"),
            unrealized_pnl=Decimal("-450.0"),
        )
        pos = HedgedPosition(
            position_id="pos_db_1",
            strategy_type=StrategyType.FUNDING_BASIS,
            status=HedgedPositionStatus.OPEN,
            leg_a=leg_a,
            leg_b=leg_b,
            net_delta_usd=Decimal("50.0"),
            entry_spread_or_basis_bps=Decimal("20.0"),
            current_unrealized_pnl_quote=Decimal("50.0"),
            realized_funding_pnl_quote=Decimal("15.2"),
        )

        saved = await ArbitrageRepository.save_position(session, pos)
        assert saved.position_id == "pos_db_1"

        retrieved = await ArbitrageRepository.get_hedged_position(session, "pos_db_1")
        assert retrieved is not None
        assert retrieved.position_id == "pos_db_1"
        assert retrieved.status == "OPEN"
        assert retrieved.realized_funding_pnl_quote == Decimal("15.2")


@pytest.mark.asyncio
async def test_dual_queue_critical_vs_telemetry_dropping():
    # Small telemetry queue size (3 items)
    writer = DualQueuePersistenceWriter(session_factory=None, telemetry_queue_maxsize=3)

    # Enqueue 5 critical items -> all 5 must be preserved
    for i in range(5):
        await writer.enqueue_critical(f"critical_{i}")
    assert writer.critical_queue.qsize() == 5

    # Enqueue 10 telemetry items -> 3 accepted, 7 dropped without error or blocking
    for i in range(10):
        await writer.enqueue_telemetry(f"telemetry_{i}")

    assert writer.telemetry_queue.qsize() == 3
    assert writer.dropped_telemetry_count == 7

    # Flush in-memory
    await writer.flush()
    assert writer.critical_queue.empty()
    assert writer.telemetry_queue.empty()
    assert writer.persisted_critical_count == 5
    assert writer.persisted_telemetry_count == 3


@pytest.mark.asyncio
async def test_dual_queue_writer_with_database(db_session_factory):
    writer = DualQueuePersistenceWriter(session_factory=db_session_factory, flush_interval_sec=0.1)
    await writer.start()

    # Enqueue executable opportunity (routes to critical queue)
    opp = ArbitrageOpportunity(
        id="opp_worker_1",
        strategy_type=StrategyType.CROSS_EXCHANGE,
        lifecycle_state=OpportunityLifecycleState.EXECUTABLE_OPPORTUNITY,
        symbol_legs=["BTC/USDT", "BTC/USDT"],
        legs=[],
        gross_edge_bps=Decimal("30.0"),
        net_edge_bps=Decimal("16.0"),
        target_notional=Decimal("500.0"),
        expected_net_profit=Decimal("0.8"),
    )
    await writer.enqueue_opportunity(opp)

    # Enqueue orderbook snapshot (routes to telemetry queue)
    book = NormalizedOrderBook(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT",
        asks=[OrderBookLevel(price=Decimal("50000.0"), quantity=Decimal("1.0"))],
        bids=[OrderBookLevel(price=Decimal("49990.0"), quantity=Decimal("1.0"))],
    )
    await writer.enqueue_order_book_snapshot(book)

    # Stop and flush writer
    await writer.stop()

    assert writer.critical_queue.empty()
    assert writer.telemetry_queue.empty()

    # Verify persisted in DB
    async with db_session_factory() as session:
        retrieved_opp = await ArbitrageRepository.get_opportunity(session, "opp_worker_1")
        assert retrieved_opp is not None
        assert retrieved_opp.id == "opp_worker_1"
