"""Deterministic Chaos and Failure Scenarios Simulation Suite.

Tests 4 critical institutional failure modes:
1. Positive Gross / Negative Net Spread Trap:
   Top-of-book prices appear profitable (gross spread > 0), but after L2 order book
   depth walk and taker fees on both legs, net edge is negative (-40 bps).
   Evaluator flags opportunity as THEORETICAL_OPPORTUNITY, 0 orders submitted, 0 balance change.
2. Stale Order Book / Desync Rejection:
   Receipt timestamp age exceeds 250ms threshold, or book is marked is_stale=True.
   Rejected at Evaluator book freshness check and RiskManager Gate 3 (DATA_STALE / BOOK_STALE).
3. Multi-Leg Partial Failure & Directional Unwind:
   In Cross-Exchange Arbitrage: Leg 1 fills on Binance; Leg 2 fails (e.g. order book depth
   exhausted on Kraken). State machine transitions to UNWINDING -> executes directional ±20 bps
   IOC unwind on Binance -> transitions to UNWOUND -> balance locks restored to 0.
4. Catastrophic Unwind Slippage & Daily Loss Limit Kill-Switch:
   - Realized emergency unwind slippage > 50 bps trips CentralRiskManager Kill Switch ->
     subsequent opportunities blocked at Gate 1 (KILL_SWITCH_ACTIVE).
   - Cumulative daily losses >= $100 USDT trip CentralRiskManager Gate 7 (DAILY_LOSS_LIMIT_BREACHED)
     and activate Kill Switch.
"""

import time
from decimal import Decimal
import pytest

from config.settings import SystemSettings
from src.core.models import (
    ArbitrageLeg,
    ArbitrageOpportunity,
    ExchangeId,
    ExecutionLegState,
    ExecutionLegStatus,
    ExecutionOrder,
    ExecutionPlan,
    ExecutionPlanStatus,
    NormalizedOrderBook,
    OpportunityLifecycleState,
    OrderBookLevel,
    OrderSide,
    StrategyType,
    SymbolMetadata,
)
from src.execution.paper_executor import PaperExecutor
from src.main import ArbitrageSystemOrchestrator
from src.market_data.market_registry import MarketRegistry
from src.opportunity.evaluator import OpportunityEvaluator
from src.portfolio.balances import BalanceManager
from src.risk.risk_manager import CentralRiskManager


class MockOrderBookEngine:
    """Mock order book engine supplying controlled L2 books for execution tests."""

    def __init__(self) -> None:
        self._books: dict[tuple[ExchangeId, str], NormalizedOrderBook] = {}

    def set_book(self, exchange: ExchangeId, symbol: str, book: NormalizedOrderBook) -> None:
        self._books[(exchange, symbol)] = book

    def get_book(self, exchange: ExchangeId, symbol: str) -> NormalizedOrderBook | None:
        return self._books.get((exchange, symbol))


@pytest.fixture
def orchestrator() -> ArbitrageSystemOrchestrator:
    settings = SystemSettings.load()
    orch = ArbitrageSystemOrchestrator(settings=settings)
    return orch


# ==============================================================================
# Scenario 1: Positive Gross / Negative Net Spread Trap
# ==============================================================================


@pytest.mark.asyncio
async def test_trap_positive_gross_negative_net_spread(
    orchestrator: ArbitrageSystemOrchestrator,
) -> None:
    """Top-of-book gross edge is positive (+10 bps) but net edge is negative (-40 bps).

    Verify: Evaluator categorizes as THEORETICAL_OPPORTUNITY, 0 orders submitted, 0 balance change.
    """
    await orchestrator.start()
    now_ms = int(time.time() * 1000)

    # Initial balances
    b_usdt_pre = orchestrator.balance_manager.get_balance(ExchangeId.BINANCE, "USDT").free
    k_usdt_pre = orchestrator.balance_manager.get_balance(ExchangeId.KRAKEN, "USDT").free
    b_btc_pre = orchestrator.balance_manager.get_balance(ExchangeId.BINANCE, "BTC").free
    k_btc_pre = orchestrator.balance_manager.get_balance(ExchangeId.KRAKEN, "BTC").free

    # Top of book: Binance Ask 50,000 vs Kraken Bid 50,050
    # Gross edge = (50050 - 50000) / 50000 = 10 bps
    # Fees = Binance (10 bps) + Kraken (40 bps) = 50 bps
    # Net edge = 10 bps - 50 bps = -40 bps (unprofitable trap)
    book_binance = NormalizedOrderBook(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT",
        asks=[OrderBookLevel(price=Decimal("50000.0"), quantity=Decimal("1.0"))],
        bids=[OrderBookLevel(price=Decimal("49990.0"), quantity=Decimal("1.0"))],
        local_receipt_timestamp_ms=now_ms,
    )
    book_kraken = NormalizedOrderBook(
        exchange=ExchangeId.KRAKEN,
        symbol="BTC/USDT",
        asks=[OrderBookLevel(price=Decimal("50060.0"), quantity=Decimal("1.0"))],
        bids=[OrderBookLevel(price=Decimal("50050.0"), quantity=Decimal("1.0"))],
        local_receipt_timestamp_ms=now_ms,
    )

    await orchestrator.handle_orderbook_update(book_binance)
    executed_plans = await orchestrator.handle_orderbook_update(book_kraken)

    # Zero orders submitted, zero execution plans created
    assert len(executed_plans) == 0

    # Balances must remain strictly unchanged
    b_usdt_post = orchestrator.balance_manager.get_balance(ExchangeId.BINANCE, "USDT").free
    k_usdt_post = orchestrator.balance_manager.get_balance(ExchangeId.KRAKEN, "USDT").free
    b_btc_post = orchestrator.balance_manager.get_balance(ExchangeId.BINANCE, "BTC").free
    k_btc_post = orchestrator.balance_manager.get_balance(ExchangeId.KRAKEN, "BTC").free

    assert b_usdt_post == b_usdt_pre
    assert k_usdt_post == k_usdt_pre
    assert b_btc_post == b_btc_pre
    assert k_btc_post == k_btc_pre

    # Verify opportunity was detected and flagged as THEORETICAL_OPPORTUNITY
    assert len(orchestrator._recent_opportunities) >= 1
    opp = orchestrator._recent_opportunities[-1]
    assert opp.lifecycle_state == OpportunityLifecycleState.THEORETICAL_OPPORTUNITY
    assert opp.gross_edge_bps == Decimal("10.000")
    assert opp.net_edge_bps < Decimal("0")

    await orchestrator.stop()


# ==============================================================================
# Scenario 2: Stale Order Book / Desync Rejection
# ==============================================================================


@pytest.mark.asyncio
async def test_stale_orderbook_and_desync_rejection(
    orchestrator: ArbitrageSystemOrchestrator,
) -> None:
    """Verify stale or desynced order books are rejected by Evaluator and Risk Gate 3."""
    await orchestrator.start()
    now_ms = int(time.time() * 1000)

    # 1. OpportunityEvaluator freshness checks
    evaluator = OpportunityEvaluator(max_data_age_ms=250)

    # Stale book (500ms old > 250ms max age)
    book_stale = NormalizedOrderBook(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT",
        asks=[OrderBookLevel(price=Decimal("50000.0"), quantity=Decimal("1.0"))],
        bids=[OrderBookLevel(price=Decimal("49990.0"), quantity=Decimal("1.0"))],
        local_receipt_timestamp_ms=now_ms - 500,
    )
    is_valid, reason = evaluator.validate_book_freshness(book_stale, current_time_ms=now_ms)
    assert is_valid is False
    assert "exceeds threshold 250ms" in (reason or "")

    # Flagged stale book (is_stale=True)
    book_flagged = NormalizedOrderBook(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT",
        asks=[OrderBookLevel(price=Decimal("50000.0"), quantity=Decimal("1.0"))],
        bids=[OrderBookLevel(price=Decimal("49990.0"), quantity=Decimal("1.0"))],
        local_receipt_timestamp_ms=now_ms,
        is_stale=True,
    )
    is_valid_flagged, reason_flagged = evaluator.validate_book_freshness(
        book_flagged, current_time_ms=now_ms
    )
    assert is_valid_flagged is False
    assert "flagged STALE" in (reason_flagged or "")

    # Crossed book check (bid >= ask)
    book_crossed = NormalizedOrderBook(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT",
        asks=[OrderBookLevel(price=Decimal("50000.0"), quantity=Decimal("1.0"))],
        bids=[OrderBookLevel(price=Decimal("50010.0"), quantity=Decimal("1.0"))],
        local_receipt_timestamp_ms=now_ms,
    )
    is_valid_crossed, reason_crossed = evaluator.validate_book_freshness(
        book_crossed, current_time_ms=now_ms
    )
    assert is_valid_crossed is False
    assert "CROSSED" in (reason_crossed or "")

    # In orchestrator pipeline: feeding stale book yields 0 executed plans
    book_kraken = NormalizedOrderBook(
        exchange=ExchangeId.KRAKEN,
        symbol="BTC/USDT",
        asks=[OrderBookLevel(price=Decimal("50600.0"), quantity=Decimal("1.0"))],
        bids=[OrderBookLevel(price=Decimal("50500.0"), quantity=Decimal("1.0"))],
        local_receipt_timestamp_ms=now_ms,
    )
    await orchestrator.handle_orderbook_update(book_kraken)
    plans = await orchestrator.handle_orderbook_update(book_stale)
    assert len(plans) == 0

    # 2. RiskManager Gate 3 Latency and Book Stale Verification
    bm = BalanceManager()
    rm = CentralRiskManager(balance_manager=bm)

    opp_data_stale = ArbitrageOpportunity(
        id="opp_data_stale_sim",
        strategy_type=StrategyType.CROSS_EXCHANGE,
        lifecycle_state=OpportunityLifecycleState.EXECUTABLE_OPPORTUNITY,
        symbol_legs=["BTC/USDT"],
        legs=[
            ArbitrageLeg(
                leg_index=0,
                exchange=ExchangeId.BINANCE,
                symbol="BTC/USDT",
                side=OrderSide.BUY,
                expected_price=Decimal("50000"),
                expected_qty=Decimal("0.01"),
                expected_fee=Decimal("0.5"),
                fee_asset="USDT",
            )
        ],
        gross_edge_bps=Decimal("100.0"),
        net_edge_bps=Decimal("50.0"),
        target_notional=Decimal("500.0"),
        expected_net_profit=Decimal("2.5"),
        profit_asset="USDT",
        timestamp_ms=now_ms - 500,  # 500ms old
    )
    decision_age = await rm.evaluate_opportunity(opp_data_stale)
    assert decision_age.is_approved is False
    assert decision_age.rejection_code == "DATA_STALE"

    opp_book_stale = ArbitrageOpportunity(
        id="opp_book_stale_sim",
        strategy_type=StrategyType.CROSS_EXCHANGE,
        lifecycle_state=OpportunityLifecycleState.EXECUTABLE_OPPORTUNITY,
        symbol_legs=["BTC/USDT"],
        legs=[
            ArbitrageLeg(
                leg_index=0,
                exchange=ExchangeId.BINANCE,
                symbol="BTC/USDT",
                side=OrderSide.BUY,
                expected_price=Decimal("50000"),
                expected_qty=Decimal("0.01"),
                expected_fee=Decimal("0.5"),
                fee_asset="USDT",
            )
        ],
        gross_edge_bps=Decimal("100.0"),
        net_edge_bps=Decimal("50.0"),
        target_notional=Decimal("500.0"),
        expected_net_profit=Decimal("2.5"),
        profit_asset="USDT",
        timestamp_ms=now_ms,
        metadata={"is_stale": True},
    )
    decision_stale_book = await rm.evaluate_opportunity(opp_book_stale)
    assert decision_stale_book.is_approved is False
    assert decision_stale_book.rejection_code == "BOOK_STALE"

    await orchestrator.stop()


# ==============================================================================
# Scenario 3: Multi-Leg Partial Failure & Directional Unwind
# ==============================================================================


@pytest.mark.asyncio
async def test_multi_leg_partial_failure_and_directional_unwind() -> None:
    """Strategy 1 Cross-Exchange: Leg 1 fills on Binance, Leg 2 fails on Kraken.

    Verify:
    - State machine transitions: EXECUTING -> PARTIALLY_FAILED -> UNWINDING -> UNWOUND.
    - Directional IOC unwind order executed on Binance (crossing touch by -20 bps).
    - Locked balances restored to 0.
    """
    registry = MarketRegistry()
    registry.register(
        SymbolMetadata(
            exchange=ExchangeId.BINANCE,
            symbol="BTC/USDT",
            native_symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            tick_size=Decimal("0.01"),
            lot_size=Decimal("0.0001"),
            min_qty=Decimal("0.0001"),
            min_notional=Decimal("10.0"),
        )
    )
    registry.register(
        SymbolMetadata(
            exchange=ExchangeId.KRAKEN,
            symbol="BTC/USDT",
            native_symbol="XBTUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            tick_size=Decimal("0.01"),
            lot_size=Decimal("0.0001"),
            min_qty=Decimal("0.0001"),
            min_notional=Decimal("10.0"),
        )
    )

    bm = BalanceManager()
    bm.set_balance(ExchangeId.BINANCE, "USDT", free=Decimal("2000.0"))
    bm.set_balance(ExchangeId.KRAKEN, "BTC", free=Decimal("1.0"))
    rm = CentralRiskManager(bm)

    engine = MockOrderBookEngine()
    # Binance book has active liquidity
    engine.set_book(
        ExchangeId.BINANCE,
        "BTC/USDT",
        NormalizedOrderBook(
            exchange=ExchangeId.BINANCE,
            symbol="BTC/USDT",
            asks=[OrderBookLevel(price=Decimal("50000.0"), quantity=Decimal("1.0"))],
            bids=[OrderBookLevel(price=Decimal("49990.0"), quantity=Decimal("1.0"))],
        ),
    )
    # Kraken book has zero bids (liquidity vanished -> Leg 1 market order fails to fill)
    engine.set_book(
        ExchangeId.KRAKEN,
        "BTC/USDT",
        NormalizedOrderBook(
            exchange=ExchangeId.KRAKEN,
            symbol="BTC/USDT",
            asks=[OrderBookLevel(price=Decimal("50410.0"), quantity=Decimal("1.0"))],
            bids=[],
        ),
    )

    executor = PaperExecutor(
        balance_manager=bm,
        risk_manager=rm,
        order_book_engine=engine,
        market_registry=registry,
        simulated_latency_ms=0,
    )

    # Leg 0: BUY on Binance @ 50,000 (Fills)
    leg0 = ExecutionLegState(
        leg_index=0,
        order=ExecutionOrder(
            client_order_id="leg0_unwind",
            exchange=ExchangeId.BINANCE,
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            quantity=Decimal("0.01"),
            price=Decimal("50000.0"),
        ),
    )
    # Leg 1: SELL on Kraken (Fails due to zero book depth)
    leg1 = ExecutionLegState(
        leg_index=1,
        order=ExecutionOrder(
            client_order_id="leg1_fail",
            exchange=ExchangeId.KRAKEN,
            symbol="BTC/USDT",
            side=OrderSide.SELL,
            quantity=Decimal("0.01"),
            price=None,
        ),
    )
    plan = ExecutionPlan(
        plan_id="plan_partial_fail_unwind",
        opportunity_id="opp_partial_fail",
        strategy_type=StrategyType.CROSS_EXCHANGE,
        status=ExecutionPlanStatus.PENDING,
        legs=[leg0, leg1],
        target_notional=Decimal("500.0"),
    )

    result_plan = await executor.execute_plan(plan)

    # State machine must conclude in UNWOUND state
    assert result_plan.status == ExecutionPlanStatus.UNWOUND
    assert leg0.status == ExecutionLegStatus.UNWOUND
    assert leg1.status == ExecutionLegStatus.FAILED

    # Balance locks must be fully released/settled
    assert bm.get_balance(ExchangeId.BINANCE, "USDT").locked == Decimal("0")
    assert bm.get_balance(ExchangeId.KRAKEN, "BTC").locked == Decimal("0")

    # Risk kill switch remains healthy (unwind succeeded within slippage threshold)
    assert rm.is_kill_switch_active() is False


# ==============================================================================
# Scenario 4: Catastrophic Unwind Slippage & Daily Loss Limit Kill-Switch
# ==============================================================================


@pytest.mark.asyncio
async def test_catastrophic_unwind_slippage_circuit_breaker() -> None:
    """When emergency unwind experiences realized slippage > 50 bps:

    Verify:
    - Plan transitions to FAILED.
    - CentralRiskManager Kill Switch is tripped.
    - Subsequent opportunities are blocked at Gate 1 (KILL_SWITCH_ACTIVE).
    """
    registry = MarketRegistry()
    registry.register(
        SymbolMetadata(
            exchange=ExchangeId.BINANCE,
            symbol="BTC/USDT",
            native_symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            tick_size=Decimal("0.01"),
            lot_size=Decimal("0.0001"),
            min_qty=Decimal("0.0001"),
            min_notional=Decimal("10.0"),
        )
    )
    registry.register(
        SymbolMetadata(
            exchange=ExchangeId.KRAKEN,
            symbol="BTC/USDT",
            native_symbol="XBTUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            tick_size=Decimal("0.01"),
            lot_size=Decimal("0.0001"),
            min_qty=Decimal("0.0001"),
            min_notional=Decimal("10.0"),
        )
    )

    bm = BalanceManager()
    bm.set_balance(ExchangeId.BINANCE, "USDT", free=Decimal("2000.0"))
    bm.set_balance(ExchangeId.KRAKEN, "BTC", free=Decimal("1.0"))
    rm = CentralRiskManager(bm)

    engine = MockOrderBookEngine()
    # Binance book: Asks at 50,000 for entry, but bids have crashed to 49,600 (80 bps slippage)
    engine.set_book(
        ExchangeId.BINANCE,
        "BTC/USDT",
        NormalizedOrderBook(
            exchange=ExchangeId.BINANCE,
            symbol="BTC/USDT",
            asks=[OrderBookLevel(price=Decimal("50000.0"), quantity=Decimal("1.0"))],
            bids=[OrderBookLevel(price=Decimal("49600.0"), quantity=Decimal("1.0"))],
        ),
    )
    # Kraken book has no bids -> triggers unwind
    engine.set_book(
        ExchangeId.KRAKEN,
        "BTC/USDT",
        NormalizedOrderBook(
            exchange=ExchangeId.KRAKEN,
            symbol="BTC/USDT",
            asks=[OrderBookLevel(price=Decimal("50410.0"), quantity=Decimal("1.0"))],
            bids=[],
        ),
    )

    executor = PaperExecutor(
        balance_manager=bm,
        risk_manager=rm,
        order_book_engine=engine,
        market_registry=registry,
        simulated_latency_ms=0,
    )

    leg0 = ExecutionLegState(
        leg_index=0,
        order=ExecutionOrder(
            client_order_id="leg0_crash",
            exchange=ExchangeId.BINANCE,
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            quantity=Decimal("0.01"),
            price=Decimal("50000.0"),
        ),
    )
    leg1 = ExecutionLegState(
        leg_index=1,
        order=ExecutionOrder(
            client_order_id="leg1_crash",
            exchange=ExchangeId.KRAKEN,
            symbol="BTC/USDT",
            side=OrderSide.SELL,
            quantity=Decimal("0.01"),
            price=None,
        ),
    )
    plan = ExecutionPlan(
        plan_id="plan_catastrophic_slippage",
        opportunity_id="opp_catastrophic_slippage",
        strategy_type=StrategyType.CROSS_EXCHANGE,
        status=ExecutionPlanStatus.PENDING,
        legs=[leg0, leg1],
        target_notional=Decimal("500.0"),
    )

    result_plan = await executor.execute_plan(plan)

    # Must transition to FAILED due to slippage circuit breaker breach
    assert result_plan.status == ExecutionPlanStatus.FAILED
    assert rm.is_kill_switch_active() is True
    assert "slippage exceeded 50.0 bps" in (rm.kill_switch_reason or "")

    # Subsequent opportunity must be rejected at Gate 1
    dummy_opp = ArbitrageOpportunity(
        id="opp_after_slippage_trip",
        strategy_type=StrategyType.CROSS_EXCHANGE,
        lifecycle_state=OpportunityLifecycleState.EXECUTABLE_OPPORTUNITY,
        symbol_legs=["BTC/USDT"],
        legs=[
            ArbitrageLeg(
                leg_index=0,
                exchange=ExchangeId.BINANCE,
                symbol="BTC/USDT",
                side=OrderSide.BUY,
                expected_price=Decimal("50000"),
                expected_qty=Decimal("0.01"),
                expected_fee=Decimal("0.5"),
                fee_asset="USDT",
            )
        ],
        gross_edge_bps=Decimal("30.0"),
        net_edge_bps=Decimal("20.0"),
        target_notional=Decimal("500.0"),
        expected_net_profit=Decimal("1.0"),
        profit_asset="USDT",
    )
    decision = await rm.evaluate_opportunity(dummy_opp)
    assert decision.is_approved is False
    assert decision.rejection_code == "KILL_SWITCH_ACTIVE"


@pytest.mark.asyncio
async def test_daily_loss_limit_breaker_and_kill_switch() -> None:
    """When cumulative daily losses reach >= $100 USDT:

    Verify:
    - CentralRiskManager Gate 7 returns DAILY_LOSS_LIMIT_BREACHED.
    - Kill switch is activated.
    - Subsequent opportunities are blocked at Gate 1.
    """
    bm = BalanceManager()
    bm.set_balance(ExchangeId.BINANCE, "USDT", free=Decimal("2000.0"))
    rm = CentralRiskManager(bm)

    # Simulate 3 losing executions of -$35 USDT each (total -$105 USDT >= $100 limit)
    await rm.exposure_tracker.record_realized_pnl(Decimal("-35.0"))
    await rm.exposure_tracker.record_realized_pnl(Decimal("-35.0"))
    await rm.exposure_tracker.record_realized_pnl(Decimal("-35.0"))

    assert rm.exposure_tracker.is_daily_loss_breached(Decimal("100.0")) is True

    test_opp = ArbitrageOpportunity(
        id="opp_daily_loss_check",
        strategy_type=StrategyType.CROSS_EXCHANGE,
        lifecycle_state=OpportunityLifecycleState.EXECUTABLE_OPPORTUNITY,
        symbol_legs=["BTC/USDT"],
        legs=[
            ArbitrageLeg(
                leg_index=0,
                exchange=ExchangeId.BINANCE,
                symbol="BTC/USDT",
                side=OrderSide.BUY,
                expected_price=Decimal("50000"),
                expected_qty=Decimal("0.01"),
                expected_fee=Decimal("0.5"),
                fee_asset="USDT",
            )
        ],
        gross_edge_bps=Decimal("30.0"),
        net_edge_bps=Decimal("20.0"),
        target_notional=Decimal("500.0"),
        expected_net_profit=Decimal("1.0"),
        profit_asset="USDT",
    )

    # First evaluation breaches Gate 7 and activates kill switch
    decision_gate7 = await rm.evaluate_opportunity(test_opp)
    assert decision_gate7.is_approved is False
    assert decision_gate7.rejection_code == "DAILY_LOSS_LIMIT_BREACHED"
    assert rm.is_kill_switch_active() is True

    # Subsequent evaluation immediately blocked by Gate 1 Kill Switch
    decision_gate1 = await rm.evaluate_opportunity(test_opp)
    assert decision_gate1.is_approved is False
    assert decision_gate1.rejection_code == "KILL_SWITCH_ACTIVE"
