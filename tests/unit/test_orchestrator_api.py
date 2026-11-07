"""Unit tests for Central Autonomous Orchestrator and FastAPI control endpoints."""

import time
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient

from config.settings import SystemSettings
from src.api.routes import create_app
from src.core.models import (
    ExchangeId,
    ExecutionPlanStatus,
    FundingRateSnapshot,
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
    return orch


@pytest.fixture
def api_client(orchestrator: ArbitrageSystemOrchestrator) -> TestClient:
    app = create_app(orchestrator)
    return TestClient(app)


@pytest.mark.asyncio
async def test_orchestrator_tick_to_trade_cross_exchange(orchestrator: ArbitrageSystemOrchestrator):
    await orchestrator.start()

    # 1. Provide Binance book (Ask = 50,000)
    book_binance = NormalizedOrderBook(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT",
        asks=[OrderBookLevel(price=Decimal("50000.0"), quantity=Decimal("1.0"))],
        bids=[OrderBookLevel(price=Decimal("49990.0"), quantity=Decimal("1.0"))],
        local_receipt_timestamp_ms=int(time.time() * 1000),
    )
    # 2. Provide Kraken book (Bid = 50,400 -> 80 bps spread > 50 bps fees + 15 bps min edge)
    book_kraken = NormalizedOrderBook(
        exchange=ExchangeId.KRAKEN,
        symbol="BTC/USDT",
        asks=[OrderBookLevel(price=Decimal("50410.0"), quantity=Decimal("1.0"))],
        bids=[OrderBookLevel(price=Decimal("50400.0"), quantity=Decimal("1.0"))],
        local_receipt_timestamp_ms=int(time.time() * 1000),
    )

    # First update registers Binance book in strategy
    plans1 = await orchestrator.handle_orderbook_update(book_binance)
    assert len(plans1) == 0

    # Second update on Kraken triggers cross-exchange opportunity detection and execution
    plans2 = await orchestrator.handle_orderbook_update(book_kraken)
    assert len(plans2) == 1

    plan = plans2[0]
    assert plan.status == ExecutionPlanStatus.COMPLETED
    assert plan.strategy_type in (StrategyType.CROSS_EXCHANGE, StrategyType.CROSS_EXCHANGE_SPOT)
    assert plan.realized_pnl > Decimal("0")
    assert len(plan.legs) == 2
    assert plan.legs[0].filled_qty > Decimal("0")
    assert plan.legs[1].filled_qty > Decimal("0")

    # Verify telemetry queues recorded opportunity and plan
    assert len(orchestrator.get_recent_opportunities()) >= 1
    assert len(orchestrator.get_recent_executions()) >= 1

    await orchestrator.stop()


@pytest.mark.asyncio
async def test_orchestrator_funding_update(orchestrator: ArbitrageSystemOrchestrator):
    await orchestrator.start()

    snapshot = FundingRateSnapshot(
        exchange=ExchangeId.BINANCE,
        symbol="BTC/USDT:USDT",
        mark_price=Decimal("50200.0"),
        index_price=Decimal("50000.0"),
        current_funding_rate=Decimal("0.0005"),
    )
    await orchestrator.handle_funding_update(snapshot)
    health = orchestrator.get_health_status()
    assert health["persistence_queue_depth"]["telemetry_queue"] >= 1

    await orchestrator.stop()


def test_api_health_and_portfolio_endpoints(api_client: TestClient):
    # Test GET /health
    resp_health = api_client.get("/health")
    assert resp_health.status_code == 200
    data_health = resp_health.json()
    assert data_health["status"] == "HEALTHY"
    assert data_health["execution_mode"] == "PAPER"
    assert data_health["kill_switch_active"] is False

    # Test GET /portfolio
    resp_port = api_client.get("/portfolio")
    assert resp_port.status_code == 200
    data_port = resp_port.json()
    assert "balances" in data_port
    assert len(data_port["balances"]) > 0
    assert "skew" in data_port
    assert "USDT" in data_port["skew"]


def test_api_kill_switch_activation_and_reset(api_client: TestClient, orchestrator: ArbitrageSystemOrchestrator):
    # 1. Activate kill switch via API
    resp_act = api_client.post(
        "/risk/kill-switch",
        json={"action": "ACTIVATE", "reason": "Operator maintenance test"},
    )
    assert resp_act.status_code == 200
    data_act = resp_act.json()
    assert data_act["kill_switch_active"] is True
    assert data_act["kill_switch_reason"] == "Operator maintenance test"

    # 2. Check /health reflects HALTED status
    resp_health = api_client.get("/health")
    assert resp_health.json()["status"] == "HALTED"
    assert resp_health.json()["kill_switch_active"] is True

    # 3. Reset kill switch via API
    resp_reset = api_client.post(
        "/risk/kill-switch",
        json={"action": "RESET", "reason": "Operator resumed"},
    )
    assert resp_reset.status_code == 200
    assert resp_reset.json()["kill_switch_active"] is False

    # 4. Check /health is back to HEALTHY
    resp_health2 = api_client.get("/health")
    assert resp_health2.json()["status"] == "HEALTHY"
    assert resp_health2.json()["kill_switch_active"] is False
