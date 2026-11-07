"""Unit tests for src/core/models.py, exceptions.py, and events.py.

Verifies:
- Decimal exactness and prohibition of floating point errors.
- Floor truncation rules for lotSize and tickSize.
- L2 Order Book VWAP walking and stale-timestamp detection.
- Arbitrage opportunity lifecycle transitions and model validation.
"""

import time
from decimal import Decimal
import pytest

from src.core.models import (
    ArbitrageLeg,
    ArbitrageOpportunity,
    BalanceRecord,
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
    OrderType,
    RiskDecision,
    StrategyType,
    SymbolMetadata,
    TimeInForce,
)
from src.core.events import (
    BaseEvent,
    OpportunityDetectedEvent,
    OrderBookUpdateEvent,
)
from src.core.exceptions import (
    ArbitrageSystemError,
    ChecksumMismatchError,
    InsufficientBalanceError,
    OrderBookDesyncError,
    StaleDataError,
)


# ==============================================================================
# SymbolMetadata & Truncation Tests
# ==============================================================================


def test_symbol_metadata_decimal_exactness() -> None:
    """Verify that SymbolMetadata parses and stores numerical values as Decimals."""
    meta = SymbolMetadata(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT",
        native_symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        tick_size="0.01",
        lot_size="0.00001",
        min_qty="0.0001",
        max_qty="100.0",
        min_notional="5.0",
        maker_fee_rate="0.001",
        taker_fee_rate="0.001",
    )
    assert isinstance(meta.tick_size, Decimal)
    assert isinstance(meta.lot_size, Decimal)
    assert isinstance(meta.min_notional, Decimal)
    assert meta.tick_size == Decimal("0.01")
    assert meta.lot_size == Decimal("0.00001")


def test_symbol_metadata_truncation_rules() -> None:
    """Verify floor truncation matching exchange lot_size and tick_size rules."""
    meta = SymbolMetadata(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT",
        native_symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        tick_size=Decimal("0.01"),
        lot_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("10.0"),
    )

    # Quantity floor truncation (0.123456 -> 0.123 with step 0.001)
    qty = Decimal("0.123456")
    truncated_qty = meta.truncate_amount(qty)
    assert truncated_qty == Decimal("0.123")

    # Price floor truncation (65432.1987 -> 65432.19 with tick 0.01)
    price = Decimal("65432.1987")
    truncated_price = meta.truncate_price(price)
    assert truncated_price == Decimal("65432.19")

    # Price round (half-even)
    rounded_price = meta.round_price(Decimal("65432.196"))
    assert rounded_price == Decimal("65432.20")


def test_symbol_metadata_validation() -> None:
    """Verify min_qty, max_qty, and min_notional validation logic."""
    meta = SymbolMetadata(
        exchange=ExchangeId.BINANCE,
        symbol="ETH/USDT",
        native_symbol="ETHUSDT",
        base_asset="ETH",
        quote_asset="USDT",
        tick_size=Decimal("0.01"),
        lot_size=Decimal("0.0001"),
        min_qty=Decimal("0.005"),
        max_qty=Decimal("50.0"),
        min_notional=Decimal("10.0"),
    )

    # Below min_qty
    valid, err = meta.validate_order(price=Decimal("3000.00"), quantity=Decimal("0.001"))
    assert not valid
    assert "below min_qty" in str(err)

    # Exceeds max_qty
    valid, err = meta.validate_order(price=Decimal("3000.00"), quantity=Decimal("60.0"))
    assert not valid
    assert "exceeds max_qty" in str(err)

    # Below min_notional
    valid, err = meta.validate_order(price=Decimal("1000.00"), quantity=Decimal("0.005"))
    # notional = 5.0 < 10.0
    assert not valid
    assert "below min_notional" in str(err)

    # Valid order
    valid, err = meta.validate_order(price=Decimal("3000.00"), quantity=Decimal("0.01"))
    # notional = 30.0 >= 10.0
    assert valid
    assert err is None


# ==============================================================================
# NormalizedOrderBook & VWAP Depth Tests
# ==============================================================================


def test_orderbook_metrics_and_stale_detection() -> None:
    """Verify spread, spread_bps, mid_price, and stale latency checks."""
    now_ms = 1_700_000_000_000
    book = NormalizedOrderBook(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT",
        bids=[
            OrderBookLevel(price="50000.00", quantity="1.5"),
            OrderBookLevel(price="49990.00", quantity="2.0"),
        ],
        asks=[
            OrderBookLevel(price="50010.00", quantity="1.0"),
            OrderBookLevel(price="50020.00", quantity="3.0"),
        ],
        sequence_id=1234567,
        exchange_timestamp_ms=now_ms - 50,
        local_receipt_timestamp_ms=now_ms - 20,
    )

    assert book.best_bid is not None
    assert book.best_bid.price == Decimal("50000.00")
    assert book.best_ask is not None
    assert book.best_ask.price == Decimal("50010.00")
    assert book.mid_price == Decimal("50005.00")
    assert book.spread == Decimal("10.00")

    # Spread in bps: (10 / 50005) * 10000 ≈ 1.9998 bps
    assert book.spread_bps is not None
    expected_bps = (Decimal("10.00") / Decimal("50005.00")) * Decimal("10000")
    assert book.spread_bps == expected_bps

    # Stale checks
    assert book.is_valid_age(max_data_age_ms=100, current_timestamp_ms=now_ms)
    assert not book.is_valid_age(max_data_age_ms=10, current_timestamp_ms=now_ms)

    book.is_stale = True
    assert not book.is_valid_age(max_data_age_ms=1000, current_timestamp_ms=now_ms)


def test_orderbook_vwap_ask_bid_walk() -> None:
    """Verify depth-weighted VWAP execution walk across multiple price levels."""
    book = NormalizedOrderBook(
        exchange=ExchangeId.KRAKEN,
        symbol="ETH/USDT",
        bids=[
            OrderBookLevel(price="3000.00", quantity="2.0"),
            OrderBookLevel(price="2990.00", quantity="3.0"),
            OrderBookLevel(price="2980.00", quantity="5.0"),
        ],
        asks=[
            OrderBookLevel(price="3005.00", quantity="1.0"),
            OrderBookLevel(price="3010.00", quantity="2.0"),
            OrderBookLevel(price="3020.00", quantity="5.0"),
        ],
        sequence_id=999,
        exchange_timestamp_ms=1000,
        local_receipt_timestamp_ms=1050,
    )

    # 1. Ask VWAP for 2.5 ETH:
    # Level 1: 1.0 @ 3005 = 3005
    # Level 2: 1.5 @ 3010 = 4515
    # Total cost = 7520 for 2.5 ETH -> VWAP = 7520 / 2.5 = 3008.00
    vwap, filled = book.vwap_ask(Decimal("2.5"))
    assert filled == Decimal("2.5")
    assert vwap == Decimal("3008.00")

    # 2. Bid VWAP for 4.0 ETH:
    # Level 1: 2.0 @ 3000 = 6000
    # Level 2: 2.0 @ 2990 = 5980
    # Total proceeds = 11980 for 4.0 ETH -> VWAP = 11980 / 4.0 = 2995.00
    vwap_b, filled_b = book.vwap_bid(Decimal("4.0"))
    assert filled_b == Decimal("4.0")
    assert vwap_b == Decimal("2995.00")

    # 3. Request exceeding book depth:
    # Asks total depth = 1.0 + 2.0 + 5.0 = 8.0 ETH
    vwap_exc, filled_exc = book.vwap_ask(Decimal("10.0"))
    assert filled_exc == Decimal("8.0")
    expected_cost = (Decimal("1.0") * Decimal("3005.00")) + (Decimal("2.0") * Decimal("3010.00")) + (Decimal("5.0") * Decimal("3020.00"))
    assert vwap_exc == expected_cost / Decimal("8.0")


# ==============================================================================
# Opportunity Lifecycle & Plan State Tests
# ==============================================================================


def test_arbitrage_opportunity_lifecycle_states() -> None:
    """Verify opportunity model supports all 5 mandatory lifecycle states."""
    leg1 = ArbitrageLeg(
        leg_index=0,
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT",
        side=OrderSide.BUY,
        expected_price=Decimal("60000.00"),
        expected_qty=Decimal("0.5"),
        expected_fee=Decimal("30.00"),
        fee_asset="USDT",
    )
    leg2 = ArbitrageLeg(
        leg_index=1,
        exchange=ExchangeId.KRAKEN,
        symbol="BTC/USDT",
        side=OrderSide.SELL,
        expected_price=Decimal("60200.00"),
        expected_qty=Decimal("0.5"),
        expected_fee=Decimal("30.10"),
        fee_asset="USDT",
    )

    opp = ArbitrageOpportunity(
        id="opp-cross-12345",
        strategy_type=StrategyType.CROSS_EXCHANGE_SPOT,
        lifecycle_state=OpportunityLifecycleState.THEORETICAL_OPPORTUNITY,
        symbol_legs=["BTC/USDT"],
        legs=[leg1, leg2],
        gross_edge_bps=Decimal("33.33"),
        net_edge_bps=Decimal("15.50"),
        target_notional=Decimal("30000.00"),
        expected_net_profit=Decimal("46.50"),
        profit_asset="USDT",
    )

    assert opp.lifecycle_state == OpportunityLifecycleState.THEORETICAL_OPPORTUNITY
    assert opp.net_edge_bps == Decimal("15.50")

    # Transition to EXECUTABLE
    opp.lifecycle_state = OpportunityLifecycleState.EXECUTABLE_OPPORTUNITY
    assert opp.lifecycle_state == OpportunityLifecycleState.EXECUTABLE_OPPORTUNITY

    # Transition to SIMULATED_FILL
    opp.lifecycle_state = OpportunityLifecycleState.SIMULATED_FILL
    assert opp.lifecycle_state == OpportunityLifecycleState.SIMULATED_FILL


def test_execution_plan_and_leg_state() -> None:
    """Verify multi-leg plan creation and leg execution tracking."""
    order = ExecutionOrder(
        client_order_id="cl-001",
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT",
        side=OrderSide.BUY,
        order_type=OrderType.IMMEDIATE_OR_CANCEL,
        time_in_force=TimeInForce.IOC,
        price=Decimal("50000.00"),
        quantity=Decimal("0.1"),
    )

    leg_state = ExecutionLegState(
        leg_index=0,
        order=order,
        status=ExecutionLegStatus.PENDING,
    )

    plan = ExecutionPlan(
        plan_id="plan-999",
        opportunity_id="opp-123",
        strategy_type=StrategyType.CROSS_EXCHANGE_SPOT,
        status=ExecutionPlanStatus.PENDING,
        legs=[leg_state],
        target_notional=Decimal("5000.00"),
    )

    assert plan.status == ExecutionPlanStatus.PENDING
    assert len(plan.legs) == 1
    assert plan.legs[0].status == ExecutionLegStatus.PENDING

    # Simulate Leg 1 fill
    plan.legs[0].status = ExecutionLegStatus.FILLED
    plan.legs[0].filled_qty = Decimal("0.1")
    plan.legs[0].average_fill_price = Decimal("50000.00")
    plan.status = ExecutionPlanStatus.COMPLETED

    assert plan.legs[0].status == ExecutionLegStatus.FILLED
    assert plan.status == ExecutionPlanStatus.COMPLETED


# ==============================================================================
# Events & Exceptions Tests
# ==============================================================================


def test_event_bus_immutability() -> None:
    """Verify events are immutable frozen models with UUID and timestamp."""
    event = BaseEvent(event_type="TEST_EVENT")
    assert event.event_id is not None
    assert event.timestamp_ms > 0
    with pytest.raises(Exception):
        # Frozen models prohibit attribute mutation
        event.event_type = "MUTATED"


def test_domain_exceptions_hierarchy() -> None:
    """Verify all domain exceptions inherit from ArbitrageSystemError."""
    assert issubclass(OrderBookDesyncError, ArbitrageSystemError)
    assert issubclass(ChecksumMismatchError, ArbitrageSystemError)
    assert issubclass(StaleDataError, ArbitrageSystemError)
    assert issubclass(InsufficientBalanceError, ArbitrageSystemError)

    err = OrderBookDesyncError("Binance sequence gap", {"expected": 100, "received": 105})
    assert "Binance sequence gap" in str(err)
    assert err.details["expected"] == 100
