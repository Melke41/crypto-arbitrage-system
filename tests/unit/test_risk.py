"""Unit tests for CentralRiskManager and pre-trade risk gates."""

import time
from decimal import Decimal
import pytest

from src.core.models import (
    ArbitrageLeg,
    ArbitrageOpportunity,
    ExchangeId,
    OpportunityLifecycleState,
    OrderSide,
    StrategyType,
)
from src.portfolio.balances import BalanceManager
from src.risk.exposure import ExposureTracker
from src.risk.limits import RiskLimits
from src.risk.risk_manager import CentralRiskManager


@pytest.fixture
def balance_manager() -> BalanceManager:
    bm = BalanceManager()
    bm.set_balance(ExchangeId.BINANCE, "USDT", free=Decimal("5000.0"))
    bm.set_balance(ExchangeId.BINANCE, "BTC", free=Decimal("2.0"))
    bm.set_balance(ExchangeId.KRAKEN, "USDT", free=Decimal("5000.0"))
    bm.set_balance(ExchangeId.KRAKEN, "BTC", free=Decimal("2.0"))
    return bm


@pytest.fixture
def valid_opportunity() -> ArbitrageOpportunity:
    now_ms = int(time.time() * 1000)
    leg0 = ArbitrageLeg(
        leg_index=0,
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT",
        side=OrderSide.BUY,
        expected_price=Decimal("50000.0"),
        expected_qty=Decimal("0.01"),
        expected_fee=Decimal("0.5"),
        fee_asset="USDT",
    )
    leg1 = ArbitrageLeg(
        leg_index=1,
        exchange=ExchangeId.KRAKEN,
        symbol="BTC/USDT",
        side=OrderSide.SELL,
        expected_price=Decimal("50200.0"),
        expected_qty=Decimal("0.01"),
        expected_fee=Decimal("1.0"),
        fee_asset="USDT",
    )
    return ArbitrageOpportunity(
        id="opp_test_1",
        strategy_type=StrategyType.CROSS_EXCHANGE,
        lifecycle_state=OpportunityLifecycleState.EXECUTABLE_OPPORTUNITY,
        symbol_legs=["BTC/USDT", "BTC/USDT"],
        legs=[leg0, leg1],
        gross_edge_bps=Decimal("40.0"),
        net_edge_bps=Decimal("20.0"),
        target_notional=Decimal("500.0"),
        expected_net_profit=Decimal("1.0"),
        timestamp_ms=now_ms,
    )


@pytest.mark.asyncio
async def test_risk_manager_approves_valid_opportunity(balance_manager, valid_opportunity):
    rm = CentralRiskManager(balance_manager)
    decision = await rm.evaluate_opportunity(valid_opportunity)
    assert decision.is_approved is True
    assert decision.reason == "Approved"
    assert decision.adjusted_notional == valid_opportunity.target_notional


@pytest.mark.asyncio
async def test_risk_manager_rejects_theoretical_opportunity(balance_manager, valid_opportunity):
    rm = CentralRiskManager(balance_manager)
    opp = valid_opportunity.model_copy(
        update={"lifecycle_state": OpportunityLifecycleState.THEORETICAL_OPPORTUNITY}
    )
    decision = await rm.evaluate_opportunity(opp)
    assert decision.is_approved is False
    assert decision.rejection_code == "NOT_EXECUTABLE_OPPORTUNITY"


@pytest.mark.asyncio
async def test_risk_manager_rejects_stale_data(balance_manager, valid_opportunity):
    rm = CentralRiskManager(balance_manager, limits=RiskLimits(max_data_age_ms=250))
    # Opportunity timestamp 300ms in the past
    stale_time = int(time.time() * 1000) - 300
    opp = valid_opportunity.model_copy(update={"timestamp_ms": stale_time})
    decision = await rm.evaluate_opportunity(opp)
    assert decision.is_approved is False
    assert decision.rejection_code == "DATA_STALE"

    # Order book flagged stale in metadata
    fresh_time = int(time.time() * 1000)
    opp_flagged = valid_opportunity.model_copy(
        update={"timestamp_ms": fresh_time, "metadata": {"is_stale": True}}
    )
    decision_flagged = await rm.evaluate_opportunity(opp_flagged)
    assert decision_flagged.is_approved is False
    assert decision_flagged.rejection_code == "BOOK_STALE"


@pytest.mark.asyncio
async def test_risk_manager_rejects_insufficient_edge(balance_manager, valid_opportunity):
    # Strategy 1 requires 15.0 bps minimum edge
    rm = CentralRiskManager(balance_manager)
    opp = valid_opportunity.model_copy(update={"net_edge_bps": Decimal("12.0")})
    decision = await rm.evaluate_opportunity(opp)
    assert decision.is_approved is False
    assert decision.rejection_code == "INSUFFICIENT_NET_EDGE"


@pytest.mark.asyncio
async def test_risk_manager_rejects_insufficient_inventory(valid_opportunity):
    # Empty balance manager
    empty_bm = BalanceManager()
    rm = CentralRiskManager(empty_bm)
    decision = await rm.evaluate_opportunity(valid_opportunity)
    assert decision.is_approved is False
    assert decision.rejection_code == "INSUFFICIENT_INVENTORY"


@pytest.mark.asyncio
async def test_risk_manager_rejects_oversized_notional_and_max_plans(balance_manager, valid_opportunity):
    rm = CentralRiskManager(balance_manager, limits=RiskLimits(max_notional_per_trade=Decimal("1000.0"), max_open_plans=2))

    # Oversized notional ($1200 > $1000)
    opp_big = valid_opportunity.model_copy(update={"target_notional": Decimal("1200.0")})
    decision_big = await rm.evaluate_opportunity(opp_big)
    assert decision_big.is_approved is False
    assert decision_big.rejection_code == "NOTIONAL_EXCEEDS_CAP"

    # Max open plans exceeded
    await rm.exposure_tracker.add_open_plan("plan1", Decimal("500.0"))
    await rm.exposure_tracker.add_open_plan("plan2", Decimal("500.0"))
    decision_plans = await rm.evaluate_opportunity(valid_opportunity)
    assert decision_plans.is_approved is False
    assert decision_plans.rejection_code == "MAX_OPEN_PLANS_EXCEEDED"


@pytest.mark.asyncio
async def test_risk_manager_daily_loss_trips_kill_switch(balance_manager, valid_opportunity):
    tracker = ExposureTracker()
    rm = CentralRiskManager(balance_manager, limits=RiskLimits(max_daily_loss_usdt=Decimal("100.0")), exposure_tracker=tracker)

    # Accumulate loss of $105 USDT
    await tracker.record_realized_pnl(Decimal("-105.0"))
    assert rm.is_kill_switch_active() is False

    # Evaluating opportunity trips the kill switch
    decision = await rm.evaluate_opportunity(valid_opportunity)
    assert decision.is_approved is False
    assert decision.rejection_code == "DAILY_LOSS_LIMIT_BREACHED"
    assert rm.is_kill_switch_active() is True

    # Subsequent opportunities rejected by Gate 1 (KILL_SWITCH_ACTIVE)
    decision2 = await rm.evaluate_opportunity(valid_opportunity)
    assert decision2.is_approved is False
    assert decision2.rejection_code == "KILL_SWITCH_ACTIVE"


@pytest.mark.asyncio
async def test_risk_manager_derivatives_limits(balance_manager, valid_opportunity):
    rm = CentralRiskManager(balance_manager, limits=RiskLimits(max_leverage=2, min_liquidation_buffer_pct=Decimal("35.0")))

    # Funding basis opportunity with leverage 3x (cap is 2x)
    opp_fb = valid_opportunity.model_copy(
        update={
            "strategy_type": StrategyType.FUNDING_BASIS,
            "metadata": {"leverage": 3, "liquidation_buffer_pct": Decimal("40.0")},
        }
    )
    decision_lev = await rm.evaluate_opportunity(opp_fb)
    assert decision_lev.is_approved is False
    assert decision_lev.rejection_code == "EXCESSIVE_LEVERAGE"

    # Funding basis with low liquidation buffer (25% < 35%)
    opp_liq = valid_opportunity.model_copy(
        update={
            "strategy_type": StrategyType.FUNDING_BASIS,
            "metadata": {"leverage": 2, "liquidation_buffer_pct": Decimal("25.0")},
        }
    )
    decision_liq = await rm.evaluate_opportunity(opp_liq)
    assert decision_liq.is_approved is False
    assert decision_liq.rejection_code == "INSUFFICIENT_LIQUIDATION_BUFFER"
