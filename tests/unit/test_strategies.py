"""Unit tests for the 4 Quantitative Strategy Engines.

Tests:
- Strategy 1: CrossExchangeStrategy (Cross-Exchange Spot Arbitrage)
- Strategy 2: TriangularStrategy (Single-Exchange Triangular Arbitrage)
- Strategy 3: FundingBasisStrategy (Delta-Neutral Funding Rate & Basis Carry)
- Strategy 4: StatArbStrategy (Statistical Arbitrage & Cointegration Pairs)
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
    StrategyType,
    SymbolMetadata,
)
from src.market_data.market_registry import MarketRegistry
from src.opportunity.evaluator import OpportunityEvaluator
from src.strategies.cross_exchange import CrossExchangeStrategy
from src.strategies.funding_basis import FundingBasisStrategy
from src.strategies.stat_arb import StatArbPairConfig, StatArbStrategy
from src.strategies.triangular import TriangularStrategy


# ==============================================================================
# Strategy 1 Tests: Cross-Exchange Spot
# ==============================================================================


@pytest.mark.asyncio
async def test_cross_exchange_strategy_execution() -> None:
    """Verify Strategy 1 evaluates cross-exchange orderbook updates."""
    registry = MarketRegistry()
    evaluator = OpportunityEvaluator(min_net_edge_cross_bps=Decimal("15.0"))
    strategy = CrossExchangeStrategy(
        market_registry=registry,
        evaluator=evaluator,
        max_notional_per_trade=Decimal("1000.0"),
        min_net_edge_bps=Decimal("15.0"),
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
        taker_fee_rate=Decimal("0.0026"),
    )
    registry.register(meta_b)
    registry.register(meta_k)

    now_ms = int(time.time() * 1000)

    # 1. Update Binance book: Ask = 50000.00
    book_b = NormalizedOrderBook(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT",
        bids=[OrderBookLevel(price="49990.00", quantity="1.0")],
        asks=[OrderBookLevel(price="50000.00", quantity="1.0")],
        local_receipt_timestamp_ms=now_ms,
    )
    opps_b = await strategy.process_orderbook_update(book_b)
    # No Kraken book in cache yet -> 0 opps
    assert len(opps_b) == 0

    # 2. Update Kraken book: Bid = 50500.00 (Huge 100 bps spread)
    book_k = NormalizedOrderBook(
        exchange=ExchangeId.KRAKEN,
        symbol="BTC/USDT",
        bids=[OrderBookLevel(price="50500.00", quantity="1.0")],
        asks=[OrderBookLevel(price="50510.00", quantity="1.0")],
        local_receipt_timestamp_ms=now_ms,
    )
    opps_k = await strategy.process_orderbook_update(book_k)
    assert len(opps_k) >= 1
    assert opps_k[0].lifecycle_state == OpportunityLifecycleState.EXECUTABLE_OPPORTUNITY
    assert opps_k[0].net_edge_bps >= Decimal("15.0")


# ==============================================================================
# Strategy 2 Tests: Single-Exchange Triangular
# ==============================================================================


@pytest.mark.asyncio
async def test_triangular_strategy_cycle_execution() -> None:
    """Verify Strategy 2 pre-computes cycles and evaluates 3-hop execution."""
    registry = MarketRegistry()
    evaluator = OpportunityEvaluator(min_net_edge_tri_bps=Decimal("5.0"))

    # Register symbols for cycle: USDT -> BTC -> ETH -> USDT
    # Leg 1: BTC/USDT (Buy BTC with USDT)
    # Leg 2: ETH/BTC (Buy ETH with BTC)
    # Leg 3: ETH/USDT (Sell ETH for USDT)
    meta_btc_usdt = SymbolMetadata(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT",
        native_symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        tick_size=Decimal("0.01"),
        lot_size=Decimal("0.0001"),
        min_qty=Decimal("0.0001"),
        min_notional=Decimal("5.0"),
        taker_fee_rate=Decimal("0.0005"),  # 5 bps
    )
    meta_eth_btc = SymbolMetadata(
        exchange=ExchangeId.BINANCE,
        symbol="ETH/BTC",
        native_symbol="ETHBTC",
        base_asset="ETH",
        quote_asset="BTC",
        tick_size=Decimal("0.00001"),
        lot_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("0.0001"),
        taker_fee_rate=Decimal("0.0005"),
    )
    meta_eth_usdt = SymbolMetadata(
        exchange=ExchangeId.BINANCE,
        symbol="ETH/USDT",
        native_symbol="ETHUSDT",
        base_asset="ETH",
        quote_asset="USDT",
        tick_size=Decimal("0.01"),
        lot_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("5.0"),
        taker_fee_rate=Decimal("0.0005"),
    )
    registry.register(meta_btc_usdt)
    registry.register(meta_eth_btc)
    registry.register(meta_eth_usdt)

    strategy = TriangularStrategy(
        exchange=ExchangeId.BINANCE,
        market_registry=registry,
        evaluator=evaluator,
        max_notional_per_trade=Decimal("1000.0"),
        min_net_edge_bps=Decimal("5.0"),
        custom_cycles=[["USDT", "BTC", "ETH", "USDT"]],
    )
    assert len(strategy._cycles) == 1

    now_ms = int(time.time() * 1000)

    # Set up pricing:
    # 1. Buy BTC @ 50,000 USDT -> 1000 USDT buys 0.02 BTC
    # 2. Buy ETH with BTC @ 0.05 BTC per ETH -> 0.02 BTC buys 0.4 ETH
    # 3. Sell ETH for USDT @ 2,600 USDT per ETH -> 0.4 ETH sells for 1040 USDT (4% profit!)
    book_btc_usdt = NormalizedOrderBook(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT",
        bids=[OrderBookLevel(price="49990.00", quantity="1.0")],
        asks=[OrderBookLevel(price="50000.00", quantity="1.0")],
        local_receipt_timestamp_ms=now_ms,
    )
    book_eth_btc = NormalizedOrderBook(
        exchange=ExchangeId.BINANCE,
        symbol="ETH/BTC",
        bids=[OrderBookLevel(price="0.049", quantity="10.0")],
        asks=[OrderBookLevel(price="0.050", quantity="10.0")],
        local_receipt_timestamp_ms=now_ms,
    )
    book_eth_usdt = NormalizedOrderBook(
        exchange=ExchangeId.BINANCE,
        symbol="ETH/USDT",
        bids=[OrderBookLevel(price="2600.00", quantity="10.0")],
        asks=[OrderBookLevel(price="2610.00", quantity="10.0")],
        local_receipt_timestamp_ms=now_ms,
    )

    await strategy.process_orderbook_update(book_btc_usdt)
    await strategy.process_orderbook_update(book_eth_btc)
    opps = await strategy.process_orderbook_update(book_eth_usdt)

    assert len(opps) == 1
    assert opps[0].lifecycle_state == OpportunityLifecycleState.EXECUTABLE_OPPORTUNITY
    assert opps[0].expected_net_profit > Decimal("0")


# ==============================================================================
# Strategy 3 Tests: Funding Basis Carry
# ==============================================================================


@pytest.mark.asyncio
async def test_funding_basis_strategy_entry_and_exit() -> None:
    """Verify Strategy 3 funding carry detection and exit signal logic."""
    registry = MarketRegistry()
    strategy = FundingBasisStrategy(
        market_registry=registry,
        max_notional_per_trade=Decimal("1000.0"),
        min_funding_bps_per_8h=Decimal("3.0"),
        min_annualized_yield_pct=Decimal("15.0"),
        basis_convergence_target_bps=Decimal("2.0"),
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
        min_notional=Decimal("10.0"),
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
        min_notional=Decimal("10.0"),
        taker_fee_rate=Decimal("0.0005"),
    )
    registry.register(meta_spot)
    registry.register(meta_perp)

    now_ms = int(time.time() * 1000)

    # Funding snapshot: 4 bps per 8h (43.8% ann yield), basis = 10 bps
    snap_pos = FundingRateSnapshot(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT:USDT",
        mark_price=Decimal("50050.00"),
        index_price=Decimal("50000.00"),
        spot_mid_price=Decimal("50000.00"),
        current_funding_rate=Decimal("0.0004"),
        funding_interval_hours=8,
    )
    strategy.update_funding_snapshot(snap_pos)

    book_spot = NormalizedOrderBook(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT",
        bids=[OrderBookLevel(price="49990.00", quantity="1.0")],
        asks=[OrderBookLevel(price="50000.00", quantity="1.0")],
        local_receipt_timestamp_ms=now_ms,
    )
    book_perp = NormalizedOrderBook(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT:USDT",
        bids=[OrderBookLevel(price="50050.00", quantity="1.0")],
        asks=[OrderBookLevel(price="50060.00", quantity="1.0")],
        local_receipt_timestamp_ms=now_ms,
    )

    await strategy.process_orderbook_update(book_spot)
    opps = await strategy.process_orderbook_update(book_perp)
    assert len(opps) == 1
    assert opps[0].strategy_type == StrategyType.FUNDING_BASIS

    # Test exit trigger when funding rate turns negative
    snap_neg = FundingRateSnapshot(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT:USDT",
        mark_price=Decimal("50000.00"),
        index_price=Decimal("50000.00"),
        spot_mid_price=Decimal("50000.00"),
        current_funding_rate=Decimal("-0.0001"),  # Negative funding!
        funding_interval_hours=8,
    )
    should_exit, reason = strategy.should_exit_position(None, snap_neg)
    assert should_exit
    assert "Negative funding rate" in reason


# ==============================================================================
# Strategy 4 Tests: Statistical Arbitrage
# ==============================================================================


@pytest.mark.asyncio
async def test_stat_arb_strategy_z_score_trigger() -> None:
    """Verify Strategy 4 evaluates cointegrated pairs and triggers Z-score entries."""
    registry = MarketRegistry()
    meta_eth = SymbolMetadata(
        exchange=ExchangeId.BINANCE,
        symbol="ETH/USDT",
        native_symbol="ETHUSDT",
        base_asset="ETH",
        quote_asset="USDT",
        tick_size=Decimal("0.01"),
        lot_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("10.0"),
    )
    meta_btc = SymbolMetadata(
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
    registry.register(meta_eth)
    registry.register(meta_btc)

    pair_cfg = StatArbPairConfig(
        pair_id="ETH_BTC_SPREAD",
        symbol_a="ETH/USDT",
        symbol_b="BTC/USDT",
        initial_beta=Decimal("0.06"),
        z_score_entry=Decimal("2.0"),
        min_half_life_sec=Decimal("0.1"),
        max_half_life_sec=Decimal("10000.0"),
    )

    strategy = StatArbStrategy(
        market_registry=registry,
        pair_configs=[pair_cfg],
        notional_per_leg=Decimal("500.0"),
    )

    now_ms = int(time.time() * 1000)

    # Populate 40 observations of baseline spread with slight variance
    for i in range(40):
        t = now_ms - (40 - i) * 1000
        p_eth = Decimal(str(3000 + (i % 3) * 1))
        p_btc = Decimal(str(50000 + (i % 3) * 10))
        pair_cfg.buffer.append(t, p_eth, p_btc)

    # Moderate spread deviation: ETH moves to 3003.5 -> Z ≈ 2.85 (>= 2.0 entry, < 4.0 stop)
    book_eth = NormalizedOrderBook(
        exchange=ExchangeId.BINANCE,
        symbol="ETH/USDT",
        bids=[OrderBookLevel(price="3003.50", quantity="1.0")],
        asks=[OrderBookLevel(price="3004.00", quantity="1.0")],
        local_receipt_timestamp_ms=now_ms,
    )
    book_btc = NormalizedOrderBook(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT",
        bids=[OrderBookLevel(price="50000.00", quantity="1.0")],
        asks=[OrderBookLevel(price="50010.00", quantity="1.0")],
        local_receipt_timestamp_ms=now_ms,
    )

    await strategy.process_orderbook_update(book_btc)
    opps = await strategy.process_orderbook_update(book_eth)

    assert len(opps) == 1
    assert opps[0].strategy_type == StrategyType.STAT_ARB
    assert opps[0].metadata["action"] == StatArbAction.ENTER_SHORT_SPREAD.value
