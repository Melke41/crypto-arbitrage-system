"""Unit tests for portfolio, balance management, and stateful hedged positions."""

from decimal import Decimal
import pytest

from src.core.models import (
    ExchangeId,
    ExecutionLegState,
    ExecutionOrder,
    ExecutionPlan,
    ExecutionPlanStatus,
    FundingRateSnapshot,
    HedgedPositionStatus,
    InstrumentType,
    OrderSide,
    OrderType,
    StrategyType,
)
from src.portfolio.balances import BalanceManager
from src.portfolio.inventory import InventoryBalancer
from src.portfolio.positions import HedgedPositionManager


@pytest.mark.asyncio
async def test_balance_manager_locking_and_double_spend():
    mgr = BalanceManager()
    mgr.set_balance(ExchangeId.BINANCE, "USDT", free=Decimal("1000.0"), locked=Decimal("0"))

    # Initial state
    bal = mgr.get_balance(ExchangeId.BINANCE, "USDT")
    assert bal.free == Decimal("1000.0")
    assert bal.locked == Decimal("0")

    # Lock 600 USDT
    success1 = await mgr.lock_funds(ExchangeId.BINANCE, "USDT", Decimal("600.0"))
    assert success1 is True
    bal = mgr.get_balance(ExchangeId.BINANCE, "USDT")
    assert bal.free == Decimal("400.0")
    assert bal.locked == Decimal("600.0")

    # Attempt to lock 500 USDT (fails due to insufficient free balance -> double spend prevented)
    success2 = await mgr.lock_funds(ExchangeId.BINANCE, "USDT", Decimal("500.0"))
    assert success2 is False
    bal = mgr.get_balance(ExchangeId.BINANCE, "USDT")
    assert bal.free == Decimal("400.0")

    # Release 200 USDT from locked
    released = await mgr.release_locked(ExchangeId.BINANCE, "USDT", Decimal("200.0"))
    assert released is True
    bal = mgr.get_balance(ExchangeId.BINANCE, "USDT")
    assert bal.free == Decimal("600.0")
    assert bal.locked == Decimal("400.0")


@pytest.mark.asyncio
async def test_settle_fill_buy_and_sell():
    mgr = BalanceManager()
    # Pre-fund Binance with locked quote and Kraken with locked base
    mgr.set_balance(ExchangeId.BINANCE, "USDT", free=Decimal("0"), locked=Decimal("50000.0"))
    mgr.set_balance(ExchangeId.BINANCE, "BTC", free=Decimal("0"), locked=Decimal("0"))
    mgr.set_balance(ExchangeId.KRAKEN, "BTC", free=Decimal("0"), locked=Decimal("1.0"))
    mgr.set_balance(ExchangeId.KRAKEN, "USDT", free=Decimal("0"), locked=Decimal("0"))

    # Settle BUY 1.0 BTC on Binance with 0.001 BTC base-asset fee
    await mgr.settle_fill(
        exchange=ExchangeId.BINANCE,
        base_asset="BTC",
        quote_asset="USDT",
        side=OrderSide.BUY,
        filled_qty=Decimal("1.0"),
        fill_price=Decimal("50000.0"),
        fee=Decimal("0.001"),
        fee_asset="BTC",
    )
    b_btc = mgr.get_balance(ExchangeId.BINANCE, "BTC")
    b_usdt = mgr.get_balance(ExchangeId.BINANCE, "USDT")
    assert b_btc.free == Decimal("0.999")  # 1.0 - 0.001 fee
    assert b_usdt.locked == Decimal("0")

    # Settle SELL 1.0 BTC on Kraken with 50 USDT quote-asset fee
    await mgr.settle_fill(
        exchange=ExchangeId.KRAKEN,
        base_asset="BTC",
        quote_asset="USDT",
        side=OrderSide.SELL,
        filled_qty=Decimal("1.0"),
        fill_price=Decimal("50200.0"),
        fee=Decimal("50.0"),
        fee_asset="USDT",
    )
    k_btc = mgr.get_balance(ExchangeId.KRAKEN, "BTC")
    k_usdt = mgr.get_balance(ExchangeId.KRAKEN, "USDT")
    assert k_btc.locked == Decimal("0")
    assert k_usdt.free == Decimal("50150.0")  # 50,200 - 50 fee


def test_inventory_balancer_skew_and_limits():
    mgr = BalanceManager()
    mgr.set_balance(ExchangeId.BINANCE, "USDT", free=Decimal("800.0"))
    mgr.set_balance(ExchangeId.KRAKEN, "USDT", free=Decimal("200.0"))

    balancer = InventoryBalancer(mgr)
    skew = balancer.calculate_skew("USDT")
    assert skew[ExchangeId.BINANCE] == Decimal("80.0")
    assert skew[ExchangeId.KRAKEN] == Decimal("20.0")

    # Increasing Binance by 100 brings it to 900 / 1100 = 81.8% <= 85% -> True
    assert balancer.check_skew_limit("USDT", ExchangeId.BINANCE, Decimal("100.0"), max_skew_pct=Decimal("85.0")) is True

    # Increasing Binance by 400 brings it to 1200 / 1400 = 85.7% > 85% -> False
    assert balancer.check_skew_limit("USDT", ExchangeId.BINANCE, Decimal("400.0"), max_skew_pct=Decimal("85.0")) is False


@pytest.mark.asyncio
async def test_hedged_position_lifecycle_marks_and_funding():
    pos_mgr = HedgedPositionManager()

    # Create dummy ExecutionPlan for FundingBasis
    leg0 = ExecutionLegState(
        leg_index=0,
        order=ExecutionOrder(
            client_order_id="leg0",
            exchange=ExchangeId.BINANCE,
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            quantity=Decimal("1.0"),
            price=Decimal("50000.0"),
        ),
        filled_qty=Decimal("1.0"),
        average_fill_price=Decimal("50000.0"),
    )
    leg1 = ExecutionLegState(
        leg_index=1,
        order=ExecutionOrder(
            client_order_id="leg1",
            exchange=ExchangeId.BINANCE,
            symbol="BTC/USDT:USDT",
            side=OrderSide.SELL,
            quantity=Decimal("1.0"),
            price=Decimal("50100.0"),
        ),
        filled_qty=Decimal("1.0"),
        average_fill_price=Decimal("50100.0"),
    )
    plan = ExecutionPlan(
        plan_id="plan_fb_1",
        opportunity_id="opp_1",
        strategy_type=StrategyType.FUNDING_BASIS,
        status=ExecutionPlanStatus.COMPLETED,
        legs=[leg0, leg1],
        target_notional=Decimal("50000.0"),
    )

    # 1. Open hedged position
    position = await pos_mgr.open_hedged_position(plan)
    assert position.status == HedgedPositionStatus.OPEN
    assert position.leg_a.side == OrderSide.BUY
    assert position.leg_b.side == OrderSide.SELL
    assert position.is_delta_neutral(max_drift_pct=Decimal("1.0")) is True
    assert len(pos_mgr.get_open_positions()) == 1

    # 2. Mark-to-market update
    # Spot moves to 51,000 (+1,000 PnL on buy leg)
    # Perp moves to 51,050 (-950 PnL on short leg)
    # Net unrealized = +50 USDT
    marks = {"BTC/USDT": Decimal("51000.0"), "BTC/USDT:USDT": Decimal("51050.0")}
    updated = await pos_mgr.update_position_marks(marks)
    assert len(updated) == 1
    pos = updated[0]
    assert pos.leg_a.unrealized_pnl == Decimal("1000.0")
    assert pos.leg_b.unrealized_pnl == Decimal("-950.0")
    assert pos.current_unrealized_pnl_quote == Decimal("50.0")

    # 3. Accrue funding rate payout
    # Short perp receives notional * funding_rate = 1.0 * 51,050 * 0.0001 = 5.105 USDT
    funding_snapshot = FundingRateSnapshot(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT:USDT",
        mark_price=Decimal("51050.0"),
        index_price=Decimal("51000.0"),
        current_funding_rate=Decimal("0.0001"),
    )
    await pos_mgr.accrue_funding(funding_snapshot)
    pos = pos_mgr.get_position(position.position_id)
    assert pos is not None
    assert pos.realized_funding_pnl_quote == Decimal("5.105")
    assert pos.total_pnl == Decimal("50.0") + Decimal("5.105")

    # 4. Close hedged position
    closed = await pos_mgr.close_hedged_position(position.position_id, reason="Basis converged")
    assert closed is True
    assert len(pos_mgr.get_open_positions()) == 0
    pos = pos_mgr.get_position(position.position_id)
    assert pos is not None
    assert pos.status == HedgedPositionStatus.CLOSED
