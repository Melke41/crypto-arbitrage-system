"""Unit tests for ExecutionStateMachine, PaperExecutor, and automated emergency unwinding."""

from decimal import Decimal
import pytest

from src.core.models import (
    ExchangeId,
    ExecutionLegState,
    ExecutionLegStatus,
    ExecutionOrder,
    ExecutionPlan,
    ExecutionPlanStatus,
    OrderBookLevel,
    NormalizedOrderBook,
    OrderSide,
    OrderType,
    StrategyType,
    SymbolMetadata,
)
from src.core.interfaces import OrderBookEngine
from src.execution.base_executor import LiveTradingDisabledError
from src.execution.live_executor import LiveExecutor
from src.execution.paper_executor import PaperExecutor
from src.execution.state_machine import ExecutionStateMachine, InvalidStateTransitionError
from src.market_data.market_registry import MarketRegistry
from src.portfolio.balances import BalanceManager
from src.risk.risk_manager import CentralRiskManager


class MockOrderBookEngine(OrderBookEngine):
    """Simple in-memory order book mock for test fixtures."""

    def __init__(self) -> None:
        self.books: dict[tuple[ExchangeId, str], NormalizedOrderBook] = {}

    def set_book(self, exchange: ExchangeId, symbol: str, book: NormalizedOrderBook) -> None:
        self.books[(exchange, symbol)] = book

    def get_book(self, exchange: ExchangeId, symbol: str) -> NormalizedOrderBook | None:
        return self.books.get((exchange, symbol))

    def apply_snapshot(self, snapshot: NormalizedOrderBook) -> None:
        pass

    def apply_diff(self, diff_payload: dict) -> NormalizedOrderBook:
        return NormalizedOrderBook(exchange=ExchangeId.BINANCE, symbol="BTC/USDT")

    def verify_checksum(self, exchange: ExchangeId, symbol: str, expected_checksum: int) -> bool:
        return True

    def is_stale(self, exchange: ExchangeId, symbol: str, max_age_ms: int) -> bool:
        return False


@pytest.fixture
def market_registry() -> MarketRegistry:
    reg = MarketRegistry()
    reg.register(
        SymbolMetadata(
            exchange=ExchangeId.BINANCE,
            symbol="BTC/USDT",
            native_symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            tick_size=Decimal("0.01"),
            lot_size=Decimal("0.00001"),
            min_qty=Decimal("0.0001"),
            min_notional=Decimal("5.0"),
            taker_fee_rate=Decimal("0.001"),
        )
    )
    reg.register(
        SymbolMetadata(
            exchange=ExchangeId.KRAKEN,
            symbol="BTC/USDT",
            native_symbol="XBTUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            tick_size=Decimal("0.1"),
            lot_size=Decimal("0.0001"),
            min_qty=Decimal("0.0001"),
            min_notional=Decimal("5.0"),
            taker_fee_rate=Decimal("0.004"),
        )
    )
    reg.register(
        SymbolMetadata(
            exchange=ExchangeId.BINANCE,
            symbol="ETH/BTC",
            native_symbol="ETHBTC",
            base_asset="ETH",
            quote_asset="BTC",
            tick_size=Decimal("0.00001"),
            lot_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("0.0001"),
            taker_fee_rate=Decimal("0.001"),
        )
    )
    reg.register(
        SymbolMetadata(
            exchange=ExchangeId.BINANCE,
            symbol="ETH/USDT",
            native_symbol="ETHUSDT",
            base_asset="ETH",
            quote_asset="USDT",
            tick_size=Decimal("0.01"),
            lot_size=Decimal("0.0001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5.0"),
            taker_fee_rate=Decimal("0.001"),
        )
    )
    return reg


def test_execution_state_machine_valid_and_invalid():
    plan = ExecutionPlan(
        plan_id="plan_1",
        opportunity_id="opp_1",
        strategy_type=StrategyType.CROSS_EXCHANGE,
        status=ExecutionPlanStatus.PENDING,
        target_notional=Decimal("500.0"),
    )

    # Valid: PENDING -> EXECUTING
    ExecutionStateMachine.transition_plan(plan, ExecutionPlanStatus.EXECUTING)
    assert plan.status == ExecutionPlanStatus.EXECUTING

    # Valid: EXECUTING -> COMPLETED
    ExecutionStateMachine.transition_plan(plan, ExecutionPlanStatus.COMPLETED)
    assert plan.status == ExecutionPlanStatus.COMPLETED

    # Invalid: COMPLETED -> EXECUTING (terminal state)
    with pytest.raises(InvalidStateTransitionError):
        ExecutionStateMachine.transition_plan(plan, ExecutionPlanStatus.EXECUTING)


@pytest.mark.asyncio
async def test_paper_executor_simultaneous_cross_exchange(market_registry):
    bm = BalanceManager()
    bm.set_balance(ExchangeId.BINANCE, "USDT", free=Decimal("2000.0"))
    bm.set_balance(ExchangeId.KRAKEN, "BTC", free=Decimal("1.0"))

    rm = CentralRiskManager(bm)

    # Mock order books
    book_engine = MockOrderBookEngine()
    book_engine.set_book(
        ExchangeId.BINANCE,
        "BTC/USDT",
        NormalizedOrderBook(
            exchange=ExchangeId.BINANCE,
            symbol="BTC/USDT",
            asks=[OrderBookLevel(price=Decimal("50000.0"), quantity=Decimal("1.0"))],
            bids=[OrderBookLevel(price=Decimal("49990.0"), quantity=Decimal("1.0"))],
        ),
    )
    book_engine.set_book(
        ExchangeId.KRAKEN,
        "BTC/USDT",
        NormalizedOrderBook(
            exchange=ExchangeId.KRAKEN,
            symbol="BTC/USDT",
            asks=[OrderBookLevel(price=Decimal("50410.0"), quantity=Decimal("1.0"))],
            bids=[OrderBookLevel(price=Decimal("50400.0"), quantity=Decimal("1.0"))],
        ),
    )

    executor = PaperExecutor(
        balance_manager=bm,
        risk_manager=rm,
        order_book_engine=book_engine,
        market_registry=market_registry,
        simulated_latency_ms=0,
    )

    leg0 = ExecutionLegState(
        leg_index=0,
        order=ExecutionOrder(
            client_order_id="leg0",
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
            client_order_id="leg1",
            exchange=ExchangeId.KRAKEN,
            symbol="BTC/USDT",
            side=OrderSide.SELL,
            quantity=Decimal("0.01"),
            price=Decimal("50400.0"),
        ),
    )
    plan = ExecutionPlan(
        plan_id="plan_cross_1",
        opportunity_id="opp_cross_1",
        strategy_type=StrategyType.CROSS_EXCHANGE,
        status=ExecutionPlanStatus.PENDING,
        legs=[leg0, leg1],
        target_notional=Decimal("500.0"),
    )

    result_plan = await executor.execute_plan(plan)
    assert result_plan.status == ExecutionPlanStatus.COMPLETED
    assert leg0.status == ExecutionLegStatus.FILLED
    assert leg1.status == ExecutionLegStatus.FILLED
    assert leg0.filled_qty == Decimal("0.01")
    assert leg1.filled_qty == Decimal("0.01")
    assert result_plan.realized_pnl > Decimal("0")


@pytest.mark.asyncio
async def test_paper_executor_triangular_sequential(market_registry):
    bm = BalanceManager()
    bm.set_balance(ExchangeId.BINANCE, "USDT", free=Decimal("1000.0"))
    bm.set_balance(ExchangeId.BINANCE, "BTC", free=Decimal("0"))
    bm.set_balance(ExchangeId.BINANCE, "ETH", free=Decimal("0"))

    rm = CentralRiskManager(bm)
    book_engine = MockOrderBookEngine()

    # Step 1: Buy BTC with USDT at 50,000 -> 0.01 BTC
    book_engine.set_book(
        ExchangeId.BINANCE,
        "BTC/USDT",
        NormalizedOrderBook(
            exchange=ExchangeId.BINANCE,
            symbol="BTC/USDT",
            asks=[OrderBookLevel(price=Decimal("50000.0"), quantity=Decimal("1.0"))],
            bids=[OrderBookLevel(price=Decimal("49990.0"), quantity=Decimal("1.0"))],
        ),
    )
    # Step 2: Buy ETH with BTC at 0.05 BTC -> 0.1998 ETH
    book_engine.set_book(
        ExchangeId.BINANCE,
        "ETH/BTC",
        NormalizedOrderBook(
            exchange=ExchangeId.BINANCE,
            symbol="ETH/BTC",
            asks=[OrderBookLevel(price=Decimal("0.05"), quantity=Decimal("10.0"))],
            bids=[OrderBookLevel(price=Decimal("0.0499"), quantity=Decimal("10.0"))],
        ),
    )
    # Step 3: Sell ETH for USDT at 2550 -> 509 USDT
    book_engine.set_book(
        ExchangeId.BINANCE,
        "ETH/USDT",
        NormalizedOrderBook(
            exchange=ExchangeId.BINANCE,
            symbol="ETH/USDT",
            asks=[OrderBookLevel(price=Decimal("2560.0"), quantity=Decimal("10.0"))],
            bids=[OrderBookLevel(price=Decimal("2550.0"), quantity=Decimal("10.0"))],
        ),
    )

    executor = PaperExecutor(
        balance_manager=bm,
        risk_manager=rm,
        order_book_engine=book_engine,
        market_registry=market_registry,
        simulated_latency_ms=0,
    )

    leg0 = ExecutionLegState(
        leg_index=0,
        order=ExecutionOrder(
            client_order_id="leg0",
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
            client_order_id="leg1",
            exchange=ExchangeId.BINANCE,
            symbol="ETH/BTC",
            side=OrderSide.BUY,
            quantity=Decimal("0.2"),  # will be dynamically adjusted by sequential chain
            price=Decimal("0.05"),
        ),
    )
    leg2 = ExecutionLegState(
        leg_index=2,
        order=ExecutionOrder(
            client_order_id="leg2",
            exchange=ExchangeId.BINANCE,
            symbol="ETH/USDT",
            side=OrderSide.SELL,
            quantity=Decimal("0.2"),
            price=Decimal("2550.0"),
        ),
    )
    plan = ExecutionPlan(
        plan_id="plan_tri_1",
        opportunity_id="opp_tri_1",
        strategy_type=StrategyType.TRIANGULAR,
        status=ExecutionPlanStatus.PENDING,
        legs=[leg0, leg1, leg2],
        target_notional=Decimal("500.0"),
    )

    result_plan = await executor.execute_plan(plan)
    assert result_plan.status == ExecutionPlanStatus.COMPLETED
    assert leg0.status == ExecutionLegStatus.FILLED
    assert leg1.status == ExecutionLegStatus.FILLED
    assert leg2.status == ExecutionLegStatus.FILLED


@pytest.mark.asyncio
async def test_emergency_unwind_on_leg_failure(market_registry):
    bm = BalanceManager()
    bm.set_balance(ExchangeId.BINANCE, "USDT", free=Decimal("2000.0"))
    # Kraken has 0 BTC -> Leg 1 will fail locking!
    bm.set_balance(ExchangeId.KRAKEN, "BTC", free=Decimal("0"))

    rm = CentralRiskManager(bm)
    book_engine = MockOrderBookEngine()
    book_engine.set_book(
        ExchangeId.BINANCE,
        "BTC/USDT",
        NormalizedOrderBook(
            exchange=ExchangeId.BINANCE,
            symbol="BTC/USDT",
            asks=[OrderBookLevel(price=Decimal("50000.0"), quantity=Decimal("1.0"))],
            bids=[OrderBookLevel(price=Decimal("49900.0"), quantity=Decimal("1.0"))],
        ),
    )

    executor = PaperExecutor(
        balance_manager=bm,
        risk_manager=rm,
        order_book_engine=book_engine,
        market_registry=market_registry,
        simulated_latency_ms=0,
    )

    leg0 = ExecutionLegState(
        leg_index=0,
        order=ExecutionOrder(
            client_order_id="leg0",
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
            client_order_id="leg1",
            exchange=ExchangeId.KRAKEN,
            symbol="BTC/USDT",
            side=OrderSide.SELL,
            quantity=Decimal("0.01"),
            price=Decimal("50200.0"),
        ),
    )
    plan = ExecutionPlan(
        plan_id="plan_fail_1",
        opportunity_id="opp_fail_1",
        strategy_type=StrategyType.CROSS_EXCHANGE,
        status=ExecutionPlanStatus.PENDING,
        legs=[leg0, leg1],
        target_notional=Decimal("500.0"),
    )

    result_plan = await executor.execute_plan(plan)
    # Leg 1 failed to lock funds -> plan transitioned to FAILED before submit
    assert result_plan.status == ExecutionPlanStatus.FAILED
    assert leg1.status == ExecutionLegStatus.FAILED

    # Now simulate a scenario where Leg 0 FILLED and Leg 1 FAILED
    leg0.status = ExecutionLegStatus.FILLED
    leg0.filled_qty = Decimal("0.01")
    leg0.average_fill_price = Decimal("50000.0")
    plan.status = ExecutionPlanStatus.PARTIALLY_FAILED

    # Emergency unwind flattens Leg 0
    unwound = await executor.emergency_unwind(plan, failed_leg_index=1)
    assert unwound is True
    assert plan.status == ExecutionPlanStatus.UNWOUND
    assert leg0.status == ExecutionLegStatus.UNWOUND


@pytest.mark.asyncio
async def test_emergency_unwind_slippage_trips_kill_switch():
    bm = BalanceManager()
    rm = CentralRiskManager(bm)
    # Book with huge bid drop: bid is 45,000 vs 50,000 entry (10% / 1000 bps drop > 50 bps cap)
    book_engine = MockOrderBookEngine()
    book_engine.set_book(
        ExchangeId.BINANCE,
        "BTC/USDT",
        NormalizedOrderBook(
            exchange=ExchangeId.BINANCE,
            symbol="BTC/USDT",
            asks=[OrderBookLevel(price=Decimal("50000.0"), quantity=Decimal("1.0"))],
            bids=[OrderBookLevel(price=Decimal("45000.0"), quantity=Decimal("1.0"))],
        ),
    )

    executor = PaperExecutor(
        balance_manager=bm,
        risk_manager=rm,
        order_book_engine=book_engine,
        max_unwind_slippage_bps=Decimal("50.0"),
    )

    leg0 = ExecutionLegState(
        leg_index=0,
        status=ExecutionLegStatus.FILLED,
        order=ExecutionOrder(
            client_order_id="leg0",
            exchange=ExchangeId.BINANCE,
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            quantity=Decimal("0.01"),
            price=Decimal("50000.0"),
        ),
        filled_qty=Decimal("0.01"),
        average_fill_price=Decimal("50000.0"),
    )
    plan = ExecutionPlan(
        plan_id="plan_slip_1",
        opportunity_id="opp_slip_1",
        strategy_type=StrategyType.CROSS_EXCHANGE,
        status=ExecutionPlanStatus.PARTIALLY_FAILED,
        legs=[leg0],
        target_notional=Decimal("500.0"),
    )

    # Emergency unwind fails due to excessive slippage
    unwound = await executor.emergency_unwind(plan, failed_leg_index=1)
    assert unwound is False
    assert plan.status == ExecutionPlanStatus.FAILED
    assert rm.is_kill_switch_active() is True
    assert "Emergency unwind slippage exceeded" in rm.kill_switch_reason


def test_live_executor_raises_by_default():
    bm = BalanceManager()
    rm = CentralRiskManager(bm)
    executor = LiveExecutor(bm, rm)

    plan = ExecutionPlan(
        plan_id="plan_live_1",
        opportunity_id="opp_1",
        strategy_type=StrategyType.CROSS_EXCHANGE,
        status=ExecutionPlanStatus.PENDING,
        target_notional=Decimal("500.0"),
    )

    # Must raise LiveTradingDisabledError
    with pytest.raises(LiveTradingDisabledError):
        import asyncio
        asyncio.run(executor.execute_plan(plan))
