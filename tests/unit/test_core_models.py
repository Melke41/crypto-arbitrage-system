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
    InstrumentType,
    FundingRateSnapshot,
    StatArbSignal,
    StatArbAction,
    PositionLeg,
    HedgedPosition,
    HedgedPositionStatus,
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


def test_cross_exchange_lot_matching() -> None:
    """Verify cross-exchange quantity harmonization across disparate exchange lot sizes."""
    # Binance: step 0.001, min 0.001, min_notional 10
    binance_meta = SymbolMetadata(
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
    # Kraken: step 0.0001, min 0.0002, min_notional 5
    kraken_meta = SymbolMetadata(
        exchange=ExchangeId.KRAKEN,
        symbol="BTC/USDT",
        native_symbol="XBTUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        tick_size=Decimal("0.1"),
        lot_size=Decimal("0.0001"),
        min_qty=Decimal("0.0002"),
        min_notional=Decimal("5.0"),
    )

    # 1. Target quantity 0.123456 -> should be truncated to 0.123 to satisfy Binance step 0.001
    ok, qty, err = SymbolMetadata.match_cross_exchange_quantity(
        meta_buy=binance_meta,
        meta_sell=kraken_meta,
        target_quantity=Decimal("0.123456"),
        buy_price=Decimal("60000.00"),
        sell_price=Decimal("60100.00"),
    )
    assert ok
    assert qty == Decimal("0.123")
    assert err is None

    # 2. Target quantity below min_notional: 0.001 BTC * 5000 = 5.0 < 10.0 min_notional
    ok, qty, err = SymbolMetadata.match_cross_exchange_quantity(
        meta_buy=binance_meta,
        meta_sell=kraken_meta,
        target_quantity=Decimal("0.001"),
        buy_price=Decimal("5000.00"),
        sell_price=Decimal("5050.00"),
    )
    assert not ok
    assert "constraint failed" in str(err)
    assert "min_notional" in str(err)

    # 3. Target quantity truncates to zero due to lot_size incompatibility
    ok_zero, qty_zero, err_zero = SymbolMetadata.match_cross_exchange_quantity(
        meta_buy=binance_meta,
        meta_sell=kraken_meta,
        target_quantity=Decimal("0.0001"),
        buy_price=Decimal("60000.00"),
        sell_price=Decimal("60100.00"),
    )
    assert not ok_zero
    assert "truncated to zero" in str(err_zero)


def test_configuration_loading() -> None:
    """Verify settings.py loads exchanges.yaml and strategy.yaml properly."""
    from config.settings import SystemSettings

    config = SystemSettings.load()
    assert config.execution_mode == "PAPER"
    assert not config.live_trading_enabled
    assert not config.enable_synthetic_fx_basis
    assert config.max_data_age_ms == 250
    assert "binance" in config.exchanges
    assert "kraken" in config.exchanges
    assert config.exchanges["binance"].default_taker_fee_bps == Decimal("10.0")
    assert config.strategy_a.min_net_edge_bps == Decimal("15.0")
    assert config.strategy_b.min_net_edge_bps == Decimal("10.0")
    assert config.risk_limits.max_daily_loss_usdt == Decimal("100.0")
    assert config.persistence.telemetry_queue_maxsize == 10000


# ==============================================================================
# Derivatives & Stateful Position Unit Tests (Strategies 3 & 4)
# ==============================================================================


def test_derivatives_symbol_metadata() -> None:
    """Verify SymbolMetadata supports Perpetual Futures parameters."""
    meta = SymbolMetadata(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT:USDT",
        native_symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        instrument_type=InstrumentType.LINEAR_PERP,
        tick_size=Decimal("0.10"),
        lot_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("5.0"),
        contract_size=Decimal("1"),
        max_leverage=2,
        maintenance_margin_rate=Decimal("0.005"),
        funding_interval_hours=8,
    )
    assert meta.instrument_type == InstrumentType.LINEAR_PERP
    assert meta.max_leverage == 2
    assert meta.maintenance_margin_rate == Decimal("0.005")


def test_funding_rate_snapshot_calculations() -> None:
    """Verify basis_bps, annualized yield pct, and annualized yield bps formulas."""
    # Mark price = 50100.0, Spot mid = 50000.0
    # Current 8h funding rate = 0.0001 (1 bp / 0.01% per 8h)
    # Basis: ((50100 - 50000) / 50000) * 10000 = (100 / 50000) * 10000 = 20.0 bps
    # Periods per year: (365 * 24) / 8 = 1095
    # Annualized yield %: 0.0001 * 1095 * 100 = 10.95%
    # Annualized yield bps: 0.0001 * 1095 * 10000 = 1095 bps
    snap = FundingRateSnapshot(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT:USDT",
        mark_price=Decimal("50100.00"),
        index_price=Decimal("50050.00"),
        spot_mid_price=Decimal("50000.00"),
        current_funding_rate=Decimal("0.0001"),
        funding_interval_hours=8,
    )

    assert snap.basis_bps == Decimal("20.0")
    assert snap.annualized_funding_yield_pct == Decimal("10.95")
    assert snap.annualized_funding_yield_bps == Decimal("1095")


def test_stat_arb_signal_validation() -> None:
    """Verify StatArbSignal and Z-score state assignments."""
    signal = StatArbSignal(
        pair_id="ETH_BTC_SPREAD",
        symbol_a="ETH/USDT",
        symbol_b="BTC/USDT",
        hedge_ratio_beta=Decimal("0.055"),
        spread_value=Decimal("0.082"),
        spread_mean=Decimal("0.050"),
        spread_std=Decimal("0.010"),
        z_score=Decimal("3.2"),
        action=StatArbAction.ENTER_SHORT_SPREAD,
    )

    assert signal.pair_id == "ETH_BTC_SPREAD"
    assert signal.z_score == Decimal("3.2")
    assert signal.action == StatArbAction.ENTER_SHORT_SPREAD


def test_hedged_position_delta_neutrality_and_margin() -> None:
    """Verify HedgedPosition delta-neutrality drift, liquidation distance, and total PnL."""
    leg_a = PositionLeg(
        symbol="BTC/USDT",
        exchange=ExchangeId.BINANCE,
        instrument_type=InstrumentType.SPOT,
        side=OrderSide.BUY,
        quantity=Decimal("1.0"),
        entry_price=Decimal("50000.00"),
        current_price=Decimal("50500.00"),
        unrealized_pnl=Decimal("500.00"),
    )

    leg_b = PositionLeg(
        symbol="BTC/USDT:USDT",
        exchange=ExchangeId.BINANCE,
        instrument_type=InstrumentType.LINEAR_PERP,
        side=OrderSide.SELL,
        quantity=Decimal("1.0"),
        entry_price=Decimal("50100.00"),
        current_price=Decimal("50600.00"),
        unrealized_pnl=Decimal("-500.00"),
    )

    position = HedgedPosition(
        position_id="pos-delta-001",
        strategy_type=StrategyType.FUNDING_BASIS,
        status=HedgedPositionStatus.OPEN,
        leg_a=leg_a,
        leg_b=leg_b,
        net_delta_usd=Decimal("0.0"),
        entry_spread_or_basis_bps=Decimal("20.0"),
        current_unrealized_pnl_quote=Decimal("0.0"),
        realized_funding_pnl_quote=Decimal("25.00"),  # 25 USDT funding received
        liquidation_price_leg_b=Decimal("75000.00"),
    )

    # 1. Delta neutrality: both legs are 1.0 BTC at prices ~50500 and ~50600
    # notional_a = 50500, notional_b = 50600 -> drift = (100 / 50500) * 100 = 0.198% < 1.0%
    assert position.is_delta_neutral(max_drift_pct=Decimal("1.0"))

    # 2. Liquidation distance: (75000 - 50600) / 50600 * 100 = 48.22%
    dist = position.liquidation_distance_pct(current_mark_price=Decimal("50600.00"))
    assert dist is not None
    assert dist > Decimal("45.0")

    # 3. Total PnL = unrealized (0.0) + realized funding (25.00) = 25.00 USDT
    assert position.total_pnl == Decimal("25.00")
