"""Unit tests for PerformanceAnalyzer, ReportGenerator, and Dashboard helper functions."""

from decimal import Decimal
import pytest

from dashboard.app import (
    build_dashboard_state,
    format_execution_log,
    format_opportunity_table,
    format_portfolio_summary,
)
from src.analytics.performance import (
    PerformanceAnalyzer,
    PerformanceReport,
)
from src.analytics.reporting import ReportGenerator
from src.core.models import (
    ArbitrageOpportunity,
    ExecutionLegState,
    ExecutionOrder,
    ExecutionPlan,
    ExecutionPlanStatus,
    HedgedPosition,
    HedgedPositionStatus,
    InstrumentType,
    OpportunityLifecycleState,
    OrderSide,
    PositionLeg,
    StrategyType,
)


@pytest.fixture
def sample_plans() -> list[ExecutionPlan]:
    # 3 winning trades (+10, +15, +25) and 1 unwound/losing trade (-10)
    p1 = ExecutionPlan(
        plan_id="p1",
        opportunity_id="o1",
        strategy_type=StrategyType.CROSS_EXCHANGE,
        status=ExecutionPlanStatus.COMPLETED,
        target_notional=Decimal("1000.0"),
        realized_pnl=Decimal("10.0"),
    )
    p2 = ExecutionPlan(
        plan_id="p2",
        opportunity_id="o2",
        strategy_type=StrategyType.TRIANGULAR,
        status=ExecutionPlanStatus.COMPLETED,
        target_notional=Decimal("1000.0"),
        realized_pnl=Decimal("15.0"),
    )
    p3 = ExecutionPlan(
        plan_id="p3",
        opportunity_id="o3",
        strategy_type=StrategyType.CROSS_EXCHANGE,
        status=ExecutionPlanStatus.COMPLETED,
        target_notional=Decimal("1000.0"),
        realized_pnl=Decimal("25.0"),
    )
    p4 = ExecutionPlan(
        plan_id="p4",
        opportunity_id="o4",
        strategy_type=StrategyType.CROSS_EXCHANGE,
        status=ExecutionPlanStatus.UNWOUND,
        target_notional=Decimal("1000.0"),
        realized_pnl=Decimal("-10.0"),
    )
    return [p1, p2, p3, p4]


@pytest.fixture
def sample_opportunities() -> list[ArbitrageOpportunity]:
    o1 = ArbitrageOpportunity(
        id="o1",
        strategy_type=StrategyType.CROSS_EXCHANGE,
        lifecycle_state=OpportunityLifecycleState.EXECUTABLE_OPPORTUNITY,
        symbol_legs=["BTC/USDT", "BTC/USDT"],
        legs=[],
        gross_edge_bps=Decimal("30.0"),
        net_edge_bps=Decimal("18.0"),
        target_notional=Decimal("1000.0"),
        expected_net_profit=Decimal("1.8"),
    )
    o2 = ArbitrageOpportunity(
        id="o2",
        strategy_type=StrategyType.TRIANGULAR,
        lifecycle_state=OpportunityLifecycleState.EXECUTABLE_OPPORTUNITY,
        symbol_legs=["USDT", "BTC", "ETH", "USDT"],
        legs=[],
        gross_edge_bps=Decimal("20.0"),
        net_edge_bps=Decimal("12.0"),
        target_notional=Decimal("1000.0"),
        expected_net_profit=Decimal("1.2"),
    )
    o_theo = ArbitrageOpportunity(
        id="o_theo",
        strategy_type=StrategyType.CROSS_EXCHANGE,
        lifecycle_state=OpportunityLifecycleState.THEORETICAL_OPPORTUNITY,
        symbol_legs=["BTC/USDT", "BTC/USDT"],
        legs=[],
        gross_edge_bps=Decimal("5.0"),
        net_edge_bps=Decimal("-2.0"),
        target_notional=Decimal("1000.0"),
        expected_net_profit=Decimal("-0.2"),
    )
    return [o1, o2, o_theo]


def test_performance_analyzer_metrics(sample_plans, sample_opportunities):
    report = PerformanceAnalyzer.analyze(
        opportunities=sample_opportunities,
        plans=sample_plans,
        initial_capital_usdt=Decimal("20000.0"),
    )

    # 1. PnL check: 10 + 15 + 25 - 10 = 40 USDT
    assert report.overall_pnl.net_realized_profit == Decimal("40.0")
    assert report.final_equity_usdt == Decimal("20040.0")

    # 2. Risk Metrics: 3 wins, 1 loss -> Win Rate 75%
    assert report.risk_metrics.total_trades == 4
    assert report.risk_metrics.winning_trades == 3
    assert report.risk_metrics.losing_trades == 1
    assert report.risk_metrics.win_rate_pct == Decimal("75.0")

    # Profit Factor: (10 + 15 + 25) / 10 = 50 / 10 = 5.0
    assert report.risk_metrics.profit_factor == Decimal("5.0")

    # Funnel: 1 theoretical, 2 executable, 4 approved plans, 3 completed, 1 unwound -> 25% failure rate
    assert report.funnel.total_theoretical_detected == 1
    assert report.funnel.total_executable_detected == 2
    assert report.funnel.total_completed_plans == 3
    assert report.funnel.total_unwound_or_failed_plans == 1
    assert report.funnel.execution_failure_rate_pct == Decimal("25.0")

    # Strategy breakdown
    assert "CROSS_EXCHANGE" in report.strategy_breakdown
    assert "TRIANGULAR" in report.strategy_breakdown
    assert report.strategy_breakdown["TRIANGULAR"].net_pnl_usdt == Decimal("15.0")
    assert report.strategy_breakdown["CROSS_EXCHANGE"].net_pnl_usdt == Decimal("25.0")  # 10 + 25 - 10


def test_report_generator_markdown_and_json(sample_plans, sample_opportunities):
    report = PerformanceAnalyzer.analyze(
        opportunities=sample_opportunities,
        plans=sample_plans,
    )

    # 1. Markdown Report
    md = ReportGenerator.generate_markdown_report(report)
    assert "# Quantitative Strategy Performance & Execution Report" in md
    assert "Net Realized Profit" in md
    assert "CROSS_EXCHANGE" in md
    assert "TRIANGULAR" in md

    # 2. JSON Report
    json_data = ReportGenerator.generate_json_report(report)
    assert isinstance(json_data, dict)
    assert "overall_pnl" in json_data
    assert "funnel" in json_data
    assert "risk_metrics" in json_data
    assert json_data["overall_pnl"]["net_realized_profit"] == "40.0"


def test_dashboard_helper_functions():
    health = {
        "status": "HEALTHY",
        "execution_mode": "PAPER",
        "kill_switch_active": False,
        "persistence_queue_depth": {"critical_queue": 0, "telemetry_queue": 10},
    }
    portfolio = {
        "balances": [{"exchange": "BINANCE", "asset": "USDT", "free": "10000", "locked": "0", "total": "10000"}],
        "skew": {"USDT": {"BINANCE": 60.0, "KRAKEN": 40.0}},
        "open_positions": [],
    }
    opps = [{"id": "opp1", "strategy_type": "CROSS_EXCHANGE", "lifecycle_state": "EXECUTABLE_OPPORTUNITY", "net_edge_bps": "20.0"}]
    execs = [{"plan_id": "p1", "strategy_type": "CROSS_EXCHANGE", "status": "COMPLETED", "realized_pnl": "5.0"}]

    # 1. State build
    state = build_dashboard_state(health, portfolio, opps, execs)
    assert state["status"] == "HEALTHY"
    assert state["execution_mode"] == "PAPER"
    assert state["opportunities_count"] == 1
    assert state["executions_count"] == 1

    # 2. Portfolio table format
    df_bal, skew, pos = format_portfolio_summary(portfolio)
    assert len(df_bal) == 1
    assert "asset" in df_bal.columns
    assert "USDT" in skew

    # 3. Opportunity table format
    df_opp = format_opportunity_table(opps)
    assert len(df_opp) == 1
    assert "net_bps" in df_opp.columns

    # 4. Execution log format
    df_exec = format_execution_log(execs)
    assert len(df_exec) == 1
    assert "realized_pnl" in df_exec.columns
