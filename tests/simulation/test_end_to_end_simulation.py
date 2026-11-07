"""Deterministic End-to-End Simulation Suite across all 4 Quantitative Strategies."""

import time
from decimal import Decimal
import pytest

from config.settings import SystemSettings
from src.analytics.backtester import HistoricalReplayBacktester
from src.core.models import (
    ExchangeId,
    ExecutionPlanStatus,
    FundingRateSnapshot,
    HedgedPositionStatus,
    NormalizedOrderBook,
    OpportunityLifecycleState,
    OrderBookLevel,
    StrategyType,
)
from src.main import ArbitrageSystemOrchestrator


@pytest.fixture
def orchestrator() -> ArbitrageSystemOrchestrator:
    settings = SystemSettings.load()
    orch = ArbitrageSystemOrchestrator(settings=settings)
    # Build triangular cycles with the seeded market registry
    orch.triangular_strategy._build_cycles(settings.strategy_2.initial_cycles)
    # Adjust StatArb synthetic test thresholds
    if "ETH_BTC_SPREAD" in orch.stat_arb_strategy._pairs:
        pair_cfg = orch.stat_arb_strategy._pairs["ETH_BTC_SPREAD"]
        pair_cfg.min_half_life = Decimal("0.1")
        pair_cfg.z_entry = Decimal("2.0")
        pair_cfg.beta = Decimal("0.055")
    return orch


@pytest.mark.asyncio
async def test_end_to_end_strategy_1_cross_exchange(orchestrator: ArbitrageSystemOrchestrator):
    await orchestrator.start()
    now_ms = int(time.time() * 1000)

    # Initial balance check
    b_usdt_pre = orchestrator.balance_manager.get_balance(ExchangeId.BINANCE, "USDT").free
    k_usdt_pre = orchestrator.balance_manager.get_balance(ExchangeId.KRAKEN, "USDT").free

    # Binance Ask = 50,000
    book_b = NormalizedOrderBook(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT",
        asks=[OrderBookLevel(price=Decimal("50000.0"), quantity=Decimal("1.0"))],
        bids=[OrderBookLevel(price=Decimal("49990.0"), quantity=Decimal("1.0"))],
        local_receipt_timestamp_ms=now_ms,
    )
    # Kraken Bid = 50,500 (100 bps spread vs 50 bps total taker fees)
    book_k = NormalizedOrderBook(
        exchange=ExchangeId.KRAKEN,
        symbol="BTC/USDT",
        asks=[OrderBookLevel(price=Decimal("50510.0"), quantity=Decimal("1.0"))],
        bids=[OrderBookLevel(price=Decimal("50500.0"), quantity=Decimal("1.0"))],
        local_receipt_timestamp_ms=now_ms,
    )

    await orchestrator.handle_orderbook_update(book_b)
    plans = await orchestrator.handle_orderbook_update(book_k)

    assert len(plans) == 1
    plan = plans[0]
    assert plan.status == ExecutionPlanStatus.COMPLETED
    assert plan.strategy_type in (StrategyType.CROSS_EXCHANGE, StrategyType.CROSS_EXCHANGE_SPOT)
    assert plan.realized_pnl > Decimal("0")

    # Verify inventory was settled
    b_usdt_post = orchestrator.balance_manager.get_balance(ExchangeId.BINANCE, "USDT").free
    k_usdt_post = orchestrator.balance_manager.get_balance(ExchangeId.KRAKEN, "USDT").free
    assert b_usdt_post < b_usdt_pre  # Spent USDT on Binance
    assert k_usdt_post > k_usdt_pre  # Gained USDT on Kraken

    await orchestrator.stop()


@pytest.mark.asyncio
async def test_end_to_end_strategy_2_triangular(orchestrator: ArbitrageSystemOrchestrator):
    await orchestrator.start()
    now_ms = int(time.time() * 1000)

    usdt_pre = orchestrator.balance_manager.get_balance(ExchangeId.BINANCE, "USDT").free

    # Hop 1: Buy BTC with USDT at 50,000 -> 0.02 BTC
    b_btc_usdt = NormalizedOrderBook(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT",
        asks=[OrderBookLevel(price=Decimal("50000.0"), quantity=Decimal("1.0"))],
        bids=[OrderBookLevel(price=Decimal("49990.0"), quantity=Decimal("1.0"))],
        local_receipt_timestamp_ms=now_ms,
    )
    # Hop 2: Buy ETH with BTC at 0.05 BTC -> ~0.4 ETH
    b_eth_btc = NormalizedOrderBook(
        exchange=ExchangeId.BINANCE,
        symbol="ETH/BTC",
        asks=[OrderBookLevel(price=Decimal("0.0500"), quantity=Decimal("10.0"))],
        bids=[OrderBookLevel(price=Decimal("0.0499"), quantity=Decimal("10.0"))],
        local_receipt_timestamp_ms=now_ms,
    )
    # Hop 3: Sell ETH for USDT at 2600 USDT (Profitable cycle: 50,000 * 0.05 = 2500 vs 2600)
    b_eth_usdt = NormalizedOrderBook(
        exchange=ExchangeId.BINANCE,
        symbol="ETH/USDT",
        asks=[OrderBookLevel(price=Decimal("2610.0"), quantity=Decimal("10.0"))],
        bids=[OrderBookLevel(price=Decimal("2600.0"), quantity=Decimal("10.0"))],
        local_receipt_timestamp_ms=now_ms,
    )

    await orchestrator.handle_orderbook_update(b_btc_usdt)
    await orchestrator.handle_orderbook_update(b_eth_btc)
    plans = await orchestrator.handle_orderbook_update(b_eth_usdt)

    assert len(plans) >= 1
    plan = plans[0]
    assert plan.status == ExecutionPlanStatus.COMPLETED
    assert plan.strategy_type in (StrategyType.TRIANGULAR, StrategyType.TRIANGULAR_SPOT)
    assert len(plan.legs) == 3
    assert plan.realized_pnl > Decimal("0")

    usdt_post = orchestrator.balance_manager.get_balance(ExchangeId.BINANCE, "USDT").free
    assert usdt_post > usdt_pre

    await orchestrator.stop()


@pytest.mark.asyncio
async def test_end_to_end_strategy_3_funding_basis(orchestrator: ArbitrageSystemOrchestrator):
    await orchestrator.start()
    now_ms = int(time.time() * 1000)

    # 1. Update spot and perp books
    spot_book = NormalizedOrderBook(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT",
        asks=[OrderBookLevel(price=Decimal("50000.0"), quantity=Decimal("2.0"))],
        bids=[OrderBookLevel(price=Decimal("49990.0"), quantity=Decimal("2.0"))],
        local_receipt_timestamp_ms=now_ms,
    )
    perp_book = NormalizedOrderBook(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT:USDT",
        asks=[OrderBookLevel(price=Decimal("50210.0"), quantity=Decimal("2.0"))],
        bids=[OrderBookLevel(price=Decimal("50200.0"), quantity=Decimal("2.0"))],
        local_receipt_timestamp_ms=now_ms,
    )
    # High positive funding rate (5 bps / 8h = 54.75% annualized > 15%)
    snapshot = FundingRateSnapshot(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT:USDT",
        mark_price=Decimal("50200.0"),
        index_price=Decimal("50000.0"),
        current_funding_rate=Decimal("0.0005"),
        local_receipt_timestamp_ms=now_ms,
    )

    await orchestrator.handle_funding_update(snapshot)
    await orchestrator.handle_orderbook_update(spot_book)
    plans = await orchestrator.handle_orderbook_update(perp_book)

    assert len(plans) >= 1
    plan = plans[0]
    assert plan.status == ExecutionPlanStatus.COMPLETED
    assert plan.strategy_type == StrategyType.FUNDING_BASIS

    # 2. Verify stateful HedgedPosition opened
    open_positions = orchestrator.position_manager.get_open_positions()
    assert len(open_positions) == 1
    pos = open_positions[0]
    assert pos.status == HedgedPositionStatus.OPEN
    assert pos.is_delta_neutral(max_drift_pct=Decimal("1.0")) is True

    # 3. Accrue funding payout
    await orchestrator.handle_funding_update(snapshot)
    pos_updated = orchestrator.position_manager.get_position(pos.position_id)
    assert pos_updated is not None
    assert pos_updated.realized_funding_pnl_quote > Decimal("0")

    await orchestrator.stop()


@pytest.mark.asyncio
async def test_end_to_end_strategy_4_stat_arb(orchestrator: ArbitrageSystemOrchestrator):
    await orchestrator.start()
    now_ms = int(time.time() * 1000)

    # Pair ETH/USDT and BTC/USDT in StatArbStrategy
    pair_cfg = orchestrator.stat_arb_strategy._pairs.get("ETH_BTC_SPREAD")
    assert pair_cfg is not None
    pair_cfg.min_half_life = Decimal("0.1")
    pair_cfg.z_entry = Decimal("2.0")
    pair_cfg.beta = Decimal("0.06")

    # Populate baseline history with moderate spread variance (beta=0.06)
    for i in range(40):
        val_eth = 3000 + (25 if i % 2 == 0 else -25)
        pair_cfg.buffer.append(now_ms - (40 - i) * 1000, Decimal(str(val_eth)), Decimal("50000.00"))

    _, mean_s, std_s, _ = pair_cfg.buffer.get_spread_series(Decimal("0.06"))
    target_spread = float(mean_s) + 2.5 * float(std_s)
    import math
    target_eth_p = round(math.exp(target_spread + 0.06 * math.log(50000)), 2)

    # Register BTC book so evaluator and pricing find it
    book_btc = NormalizedOrderBook(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT",
        bids=[OrderBookLevel(price=Decimal("50000.00"), quantity=Decimal("10.0"))],
        asks=[OrderBookLevel(price=Decimal("50010.00"), quantity=Decimal("10.0"))],
        local_receipt_timestamp_ms=now_ms,
    )
    # Trigger a 2.5 sigma divergence on ETH
    book_eth = NormalizedOrderBook(
        exchange=ExchangeId.BINANCE,
        symbol="ETH/USDT",
        bids=[OrderBookLevel(price=Decimal(str(target_eth_p)), quantity=Decimal("10.0"))],
        asks=[OrderBookLevel(price=Decimal(str(target_eth_p + 0.5)), quantity=Decimal("10.0"))],
        local_receipt_timestamp_ms=now_ms,
    )
    await orchestrator.handle_orderbook_update(book_btc)
    plans = await orchestrator.handle_orderbook_update(book_eth)

    assert len(plans) >= 1
    plan = plans[0]
    assert plan.status == ExecutionPlanStatus.COMPLETED
    assert plan.strategy_type == StrategyType.STAT_ARB
    assert len(plan.legs) == 2

    await orchestrator.stop()


@pytest.mark.asyncio
async def test_historical_replay_backtester_multi_strategy():
    backtester = HistoricalReplayBacktester()
    now_ms = int(time.time() * 1000)

    # Multi-strategy chronological events: Cross-Exchange + Triangular
    events = [
        NormalizedOrderBook(
            exchange=ExchangeId.BINANCE,
            symbol="BTC/USDT",
            asks=[OrderBookLevel(price=Decimal("50000.0"), quantity=Decimal("1.0"))],
            bids=[OrderBookLevel(price=Decimal("49990.0"), quantity=Decimal("1.0"))],
            local_receipt_timestamp_ms=now_ms - 200,
        ),
        NormalizedOrderBook(
            exchange=ExchangeId.KRAKEN,
            symbol="BTC/USDT",
            asks=[OrderBookLevel(price=Decimal("50410.0"), quantity=Decimal("1.0"))],
            bids=[OrderBookLevel(price=Decimal("50400.0"), quantity=Decimal("1.0"))],
            local_receipt_timestamp_ms=now_ms - 100,
        ),
    ]

    report = await backtester.run(events)

    assert report.overall_pnl.net_realized_profit > Decimal("0")
    assert report.funnel.total_completed_plans >= 1
    assert "CROSS_EXCHANGE" in report.strategy_breakdown
    assert report.risk_metrics.win_rate_pct == Decimal("100.0")
