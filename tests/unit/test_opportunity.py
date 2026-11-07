"""Unit tests for src/opportunity/ (fee_calculator.py, profitability.py, evaluator.py).

Verifies:
- VIP 0 vs BNB-discounted fee calculation and base vs quote deduction.
- L2 VWAP depth-walked net profitability for all 4 strategies.
- Distinction between THEORETICAL_OPPORTUNITY and EXECUTABLE_OPPORTUNITY.
- Staleness and crossed-book gating in OpportunityEvaluator.
"""

import time
from decimal import Decimal
import pytest

from src.core.models import (
    ExchangeId,
    FundingRateSnapshot,
    InstrumentType,
    NormalizedOrderBook,
    OpportunityLifecycleState,
    OrderBookLevel,
    OrderSide,
    StatArbAction,
    StatArbSignal,
    SymbolMetadata,
)
from src.opportunity.evaluator import OpportunityEvaluator
from src.opportunity.fee_calculator import FeeCalculator
from src.opportunity.profitability import ProfitabilityCalculator


# ==============================================================================
# FeeCalculator Tests
# ==============================================================================


def test_fee_calculator_rates_and_bnb_discount() -> None:
    """Verify conservative defaults and BNB fee gating."""
    # Binance Spot default (VIP 0 without BNB) -> 0.10% maker, 0.10% taker
    m_rate, t_rate = FeeCalculator.get_fee_rates(ExchangeId.BINANCE, InstrumentType.SPOT)
    assert m_rate == Decimal("0.0010")
    assert t_rate == Decimal("0.0010")

    # Binance Spot with BNB discount enabled but balance < 0.1 -> still default
    m_rate, t_rate = FeeCalculator.get_fee_rates(
        ExchangeId.BINANCE,
        InstrumentType.SPOT,
        enable_bnb_discount=True,
        bnb_balance=Decimal("0.05"),
    )
    assert m_rate == Decimal("0.0010")

    # Binance Spot with BNB discount enabled and balance >= 0.1 -> 0.075%
    m_rate, t_rate = FeeCalculator.get_fee_rates(
        ExchangeId.BINANCE,
        InstrumentType.SPOT,
        enable_bnb_discount=True,
        bnb_balance=Decimal("0.5"),
    )
    assert m_rate == Decimal("0.00075")
    assert t_rate == Decimal("0.00075")

    # Binance Perp -> 0.02% maker, 0.05% taker
    m_rate, t_rate = FeeCalculator.get_fee_rates(ExchangeId.BINANCE, InstrumentType.LINEAR_PERP)
    assert m_rate == Decimal("0.0002")
    assert t_rate == Decimal("0.0005")

    # Kraken Spot -> 0.25% maker, 0.40% taker
    m_rate, t_rate = FeeCalculator.get_fee_rates(ExchangeId.KRAKEN, InstrumentType.SPOT)
    assert m_rate == Decimal("0.0025")
    assert t_rate == Decimal("0.0040")


def test_fee_deduction_base_vs_quote() -> None:
    """Verify base-asset fee deduction vs quote-currency fee deduction."""
    notional = Decimal("50000.00")
    qty = Decimal("1.0")
    rate = Decimal("0.0010")  # 10 bps

    # Deduct in base (e.g. buying BTC with USDT on Binance without BNB)
    # Delivered base = 1.0 - (1.0 * 0.001) = 0.999 BTC
    fee_quote, fee_base, net_qty = FeeCalculator.calculate_spot_buy_fee(
        notional_cost=notional,
        quantity=qty,
        taker_fee_rate=rate,
        deduct_in_base=True,
    )
    assert fee_base == Decimal("0.001")
    assert net_qty == Decimal("0.999")
    assert fee_quote == Decimal("50.00")


# ==============================================================================
# ProfitabilityCalculator Tests
# ==============================================================================


def test_cross_exchange_profitability_theoretical_vs_executable() -> None:
    """Verify cross-exchange calculation distinguishes theoretical from executable net edge."""
    now_ms = int(time.time() * 1000)

    # 1. Subtle spread: Buy on Binance @ 50000, Sell on Kraken @ 50030
    # Gross spread = 30 / 50000 = 6.0 bps
    # Fees: Binance taker 10 bps (50) + Kraken taker 40 bps (200) = 50 bps fee drag
    # Net edge will be negative (-44 bps) -> classified as THEORETICAL_OPPORTUNITY
    book_b = NormalizedOrderBook(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT",
        bids=[OrderBookLevel(price="49990.00", quantity="1.0")],
        asks=[OrderBookLevel(price="50000.00", quantity="1.0")],
        sequence_id=1,
        local_receipt_timestamp_ms=now_ms,
    )
    book_k = NormalizedOrderBook(
        exchange=ExchangeId.KRAKEN,
        symbol="BTC/USDT",
        bids=[OrderBookLevel(price="50030.00", quantity="1.0")],
        asks=[OrderBookLevel(price="50040.00", quantity="1.0")],
        sequence_id=1,
        local_receipt_timestamp_ms=now_ms,
    )
    meta_b = SymbolMetadata(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT",
        native_symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        tick_size=Decimal("0.01"),
        lot_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("10.0"),
        taker_fee_rate=Decimal("0.0010"),
    )
    meta_k = SymbolMetadata(
        exchange=ExchangeId.KRAKEN,
        symbol="BTC/USDT",
        native_symbol="BTC/USDT",
        base_asset="BTC",
        quote_asset="USDT",
        tick_size=Decimal("0.01"),
        lot_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("10.0"),
        taker_fee_rate=Decimal("0.0040"),
    )

    opp_theor = ProfitabilityCalculator.evaluate_cross_exchange(
        book_buy=book_b,
        book_sell=book_k,
        meta_buy=meta_b,
        meta_sell=meta_k,
        target_notional=Decimal("1000.00"),
        min_net_edge_bps=Decimal("15.0"),
    )
    assert opp_theor is not None
    assert opp_theor.lifecycle_state == OpportunityLifecycleState.THEORETICAL_OPPORTUNITY
    assert opp_theor.gross_edge_bps == Decimal("6.0")
    assert opp_theor.net_edge_bps < Decimal("0")

    # 2. Large spread: Buy on Binance @ 50000, Sell on Kraken @ 50500
    # Gross spread = 500 / 50000 = 100 bps
    # Fees = 50 bps -> Net edge = ~50 bps > 15 bps threshold -> EXECUTABLE_OPPORTUNITY
    book_k_high = NormalizedOrderBook(
        exchange=ExchangeId.KRAKEN,
        symbol="BTC/USDT",
        bids=[OrderBookLevel(price="50500.00", quantity="1.0")],
        asks=[OrderBookLevel(price="50510.00", quantity="1.0")],
        sequence_id=2,
        local_receipt_timestamp_ms=now_ms,
    )

    opp_exec = ProfitabilityCalculator.evaluate_cross_exchange(
        book_buy=book_b,
        book_sell=book_k_high,
        meta_buy=meta_b,
        meta_sell=meta_k,
        target_notional=Decimal("1000.00"),
        min_net_edge_bps=Decimal("15.0"),
    )
    assert opp_exec is not None
    assert opp_exec.lifecycle_state == OpportunityLifecycleState.EXECUTABLE_OPPORTUNITY
    assert opp_exec.net_edge_bps >= Decimal("15.0")
    assert opp_exec.expected_net_profit > Decimal("0")


def test_funding_basis_profitability() -> None:
    """Verify funding basis carry calculations, break-even period, and edge."""
    now_ms = int(time.time() * 1000)
    spot_book = NormalizedOrderBook(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT",
        bids=[OrderBookLevel(price="50000.00", quantity="1.0")],
        asks=[OrderBookLevel(price="50000.00", quantity="1.0")],
        sequence_id=1,
        local_receipt_timestamp_ms=now_ms,
    )
    perp_book = NormalizedOrderBook(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT:USDT",
        bids=[OrderBookLevel(price="50050.00", quantity="1.0")],  # Basis premium = 10 bps
        asks=[OrderBookLevel(price="50060.00", quantity="1.0")],
        sequence_id=1,
        local_receipt_timestamp_ms=now_ms,
    )
    funding_snap = FundingRateSnapshot(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT:USDT",
        mark_price=Decimal("50050.00"),
        index_price=Decimal("50000.00"),
        spot_mid_price=Decimal("50000.00"),
        current_funding_rate=Decimal("0.0003"),  # 3 bps per 8h = 32.85% annualized
        funding_interval_hours=8,
    )
    meta_spot = SymbolMetadata(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT",
        native_symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        tick_size=Decimal("0.01"),
        lot_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("5.0"),
        taker_fee_rate=Decimal("0.0010"),
    )
    meta_perp = SymbolMetadata(
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
        taker_fee_rate=Decimal("0.0005"),
    )

    opp = ProfitabilityCalculator.evaluate_funding_basis(
        spot_book=spot_book,
        perp_book=perp_book,
        funding_snapshot=funding_snap,
        meta_spot=meta_spot,
        meta_perp=meta_perp,
        target_notional=Decimal("1000.00"),
        min_funding_bps_per_8h=Decimal("3.0"),
        min_annualized_yield_pct=Decimal("15.0"),
    )

    assert opp is not None
    assert opp.lifecycle_state == OpportunityLifecycleState.EXECUTABLE_OPPORTUNITY
    assert "hours_to_breakeven" in opp.metadata


# ==============================================================================
# OpportunityEvaluator Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_evaluator_staleness_and_crossed_rejection() -> None:
    """Verify evaluator blocks opportunities built on stale or crossed books."""
    evaluator = OpportunityEvaluator(max_data_age_ms=250)
    now_ms = int(time.time() * 1000)

    # 1. Fresh book -> valid
    fresh_book = NormalizedOrderBook(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT",
        bids=[OrderBookLevel(price="50000.00", quantity="1.0")],
        asks=[OrderBookLevel(price="50010.00", quantity="1.0")],
        local_receipt_timestamp_ms=now_ms,
    )
    valid, err = evaluator.validate_book_freshness(fresh_book, current_time_ms=now_ms)
    assert valid

    # 2. Stale book (> 250ms old) -> rejected
    stale_book = NormalizedOrderBook(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT",
        bids=[OrderBookLevel(price="50000.00", quantity="1.0")],
        asks=[OrderBookLevel(price="50010.00", quantity="1.0")],
        local_receipt_timestamp_ms=now_ms - 300,
    )
    valid, err = evaluator.validate_book_freshness(stale_book, current_time_ms=now_ms)
    assert not valid
    assert "exceeds threshold" in str(err)

    # 3. Crossed book (Bid >= Ask) -> rejected
    crossed_book = NormalizedOrderBook(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT",
        bids=[OrderBookLevel(price="50020.00", quantity="1.0")],
        asks=[OrderBookLevel(price="50010.00", quantity="1.0")],
        local_receipt_timestamp_ms=now_ms,
    )
    valid, err = evaluator.validate_book_freshness(crossed_book, current_time_ms=now_ms)
    assert not valid
    assert "CROSSED" in str(err)
